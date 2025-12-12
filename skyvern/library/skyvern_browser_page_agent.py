import asyncio
import typing
from typing import Any, Literal, overload

import structlog
from playwright.async_api import Page

from skyvern.client import GetRunResponse, SkyvernEnvironment, WorkflowRunResponse
from skyvern.client.core import RequestOptions
from skyvern.library.constants import DEFAULT_AGENT_HEARTBEAT_INTERVAL, DEFAULT_AGENT_TIMEOUT
from skyvern.schemas.run_blocks import CredentialType
from skyvern.schemas.runs import RunEngine, RunStatus, TaskRunResponse

if typing.TYPE_CHECKING:
    from skyvern.library.skyvern_browser import SkyvernBrowser

LOG = structlog.get_logger()


def _get_app_url_for_run(run_id: str) -> str:
    return f"https://app.skyvern.com/runs/{run_id}"


class SkyvernBrowserPageAgent:
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
            request_options=RequestOptions(additional_headers={"X-User-Agent": "skyvern-sdk"}),
        )
        if self._browser.skyvern.environment == SkyvernEnvironment.CLOUD:
            LOG.info("AI task is running, this may take a while", url=_get_app_url_for_run(task_run.run_id))
        else:
            LOG.info("AI task is running, this may take a while", run_id=task_run.run_id)

        task_run = await self._wait_for_run_completion(task_run.run_id, timeout)
        LOG.info("AI task finished", run_id=task_run.run_id, status=task_run.status)
        return TaskRunResponse.model_validate(task_run.model_dump())

    @overload
    async def login(
        self,
        *,
        credential_type: Literal[CredentialType.skyvern] = CredentialType.skyvern,
        credential_id: str,
        url: str | None = None,
        prompt: str | None = None,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse: ...

    @overload
    async def login(
        self,
        *,
        credential_type: Literal[CredentialType.bitwarden],
        bitwarden_item_id: str,
        bitwarden_collection_id: str | None = None,
        url: str | None = None,
        prompt: str | None = None,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse: ...

    @overload
    async def login(
        self,
        *,
        credential_type: Literal[CredentialType.onepassword],
        onepassword_vault_id: str,
        onepassword_item_id: str,
        url: str | None = None,
        prompt: str | None = None,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse: ...

    @overload
    async def login(
        self,
        *,
        credential_type: Literal[CredentialType.azure_vault],
        azure_vault_name: str,
        azure_vault_username_key: str,
        azure_vault_password_key: str,
        azure_vault_totp_secret_key: str | None = None,
        url: str | None = None,
        prompt: str | None = None,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse: ...

    async def login(
        self,
        *,
        credential_type: CredentialType = CredentialType.skyvern,
        url: str | None = None,
        credential_id: str | None = None,
        bitwarden_collection_id: str | None = None,
        bitwarden_item_id: str | None = None,
        onepassword_vault_id: str | None = None,
        onepassword_item_id: str | None = None,
        azure_vault_name: str | None = None,
        azure_vault_username_key: str | None = None,
        azure_vault_password_key: str | None = None,
        azure_vault_totp_secret_key: str | None = None,
        prompt: str | None = None,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse:
        """Run a login task in the context of this page and wait for it to finish.

        This method has multiple overloaded signatures for different credential types:

        1. Skyvern credentials (default):
            ```python
            await page.agent.login(credential_id="cred_123")
            ```

        2. Bitwarden credentials:
            ```python
            await page.agent.login(
                credential_type=CredentialType.bitwarden,
                bitwarden_collection_id="collection_id",
                bitwarden_item_id="item_id",
            )
            ```

        3. 1Password credentials:
            ```python
            await page.agent.login(
                credential_type=CredentialType.onepassword,
                onepassword_vault_id="vault_id",
                onepassword_item_id="item_id",
            )
            ```

        4. Azure Vault credentials:
            ```python
            await page.agent.login(
                credential_type=CredentialType.azure_vault,
                azure_vault_name="vault_name",
                azure_vault_username_key="username_key",
                azure_vault_password_key="password_key",
            )
            ```

        Args:
            credential_id: ID of the Skyvern credential to use (required for skyvern credential_type).
            credential_type: Type of credential store to use. Defaults to CredentialType.skyvern.
            url: URL to navigate to for login. If not provided, uses the current page URL.
            bitwarden_collection_id: Bitwarden collection ID (optional for bitwarden credential_type).
            bitwarden_item_id: Bitwarden item ID (required for bitwarden credential_type).
            onepassword_vault_id: 1Password vault ID (required for onepassword credential_type).
            onepassword_item_id: 1Password item ID (required for onepassword credential_type).
            azure_vault_name: Azure Vault name (required for azure_vault credential_type).
            azure_vault_username_key: Azure Vault username key (required for azure_vault credential_type).
            azure_vault_password_key: Azure Vault password key (required for azure_vault credential_type).
            azure_vault_totp_secret_key: Azure Vault TOTP secret key (optional for azure_vault credential_type).
            prompt: Additional instructions for the login process.
            webhook_url: URL to receive webhook notifications about login progress.
            totp_identifier: Identifier for TOTP authentication.
            totp_url: URL to fetch TOTP codes from.
            extra_http_headers: Additional HTTP headers to include in requests.
            timeout: Maximum time in seconds to wait for login completion.

        Returns:
            WorkflowRunResponse containing the login workflow execution results.
        """

        LOG.info("Starting AI login workflow", credential_type=credential_type)

        workflow_run = await self._browser.skyvern.login(
            credential_type=credential_type,
            url=url or self._get_page_url(),
            credential_id=credential_id,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_item_id=bitwarden_item_id,
            onepassword_vault_id=onepassword_vault_id,
            onepassword_item_id=onepassword_item_id,
            azure_vault_name=azure_vault_name,
            azure_vault_username_key=azure_vault_username_key,
            azure_vault_password_key=azure_vault_password_key,
            azure_vault_totp_secret_key=azure_vault_totp_secret_key,
            prompt=prompt,
            webhook_url=webhook_url,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            extra_http_headers=extra_http_headers,
            request_options=RequestOptions(additional_headers={"X-User-Agent": "skyvern-sdk"}),
        )
        if self._browser.skyvern.environment == SkyvernEnvironment.CLOUD:
            LOG.info(
                "AI login workflow is running, this may take a while", url=_get_app_url_for_run(workflow_run.run_id)
            )
        else:
            LOG.info("AI login workflow is running, this may take a while", run_id=workflow_run.run_id)

        workflow_run = await self._wait_for_run_completion(workflow_run.run_id, timeout)
        LOG.info("AI login workflow finished", run_id=workflow_run.run_id, status=workflow_run.status)
        return WorkflowRunResponse.model_validate(workflow_run.model_dump())

    async def download_files(
        self,
        prompt: str,
        *,
        url: str | None = None,
        download_suffix: str | None = None,
        download_timeout: float | None = None,
        max_steps_per_run: int | None = None,
        webhook_url: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_AGENT_TIMEOUT,
    ) -> WorkflowRunResponse:
        """Run a file download task in the context of this page and wait for it to finish.

        Args:
            prompt: Instructions for navigating to and downloading the file.
            url: URL to navigate to for file download. If not provided, uses the current page URL.
            download_suffix: Suffix or complete filename for the downloaded file.
            download_timeout: Timeout in seconds for the download operation.
            max_steps_per_run: Maximum number of steps to execute.
            webhook_url: URL to receive webhook notifications about download progress.
            totp_identifier: Identifier for TOTP authentication.
            totp_url: URL to fetch TOTP codes from.
            extra_http_headers: Additional HTTP headers to include in requests.
            timeout: Maximum time in seconds to wait for download completion.

        Returns:
            WorkflowRunResponse containing the file download workflow execution results.
        """

        LOG.info("Starting AI file download workflow", navigation_goal=prompt)

        workflow_run = await self._browser.skyvern.download_files(
            navigation_goal=prompt,
            url=url or self._get_page_url(),
            download_suffix=download_suffix,
            download_timeout=download_timeout,
            max_steps_per_run=max_steps_per_run,
            webhook_url=webhook_url,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            extra_http_headers=extra_http_headers,
            request_options=RequestOptions(additional_headers={"X-User-Agent": "skyvern-sdk"}),
        )
        LOG.info("AI file download workflow is running, this may take a while", run_id=workflow_run.run_id)

        workflow_run = await self._wait_for_run_completion(workflow_run.run_id, timeout)
        LOG.info("AI file download workflow finished", run_id=workflow_run.run_id, status=workflow_run.status)
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

        LOG.info("Starting AI workflow", workflow_id=workflow_id)

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
            request_options=RequestOptions(additional_headers={"X-User-Agent": "skyvern-sdk"}),
        )
        if self._browser.skyvern.environment == SkyvernEnvironment.CLOUD:
            LOG.info("AI workflow is running, this may take a while", url=_get_app_url_for_run(workflow_run.run_id))
        else:
            LOG.info("AI workflow is running, this may take a while", run_id=workflow_run.run_id)

        workflow_run = await self._wait_for_run_completion(workflow_run.run_id, timeout)
        LOG.info("AI workflow finished", run_id=workflow_run.run_id, status=workflow_run.status)
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
