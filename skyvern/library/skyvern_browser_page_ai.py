from typing import TYPE_CHECKING, Any

import structlog
from playwright.async_api import Page

from skyvern.client import (
    RunSdkActionRequestAction_AiAct,
    RunSdkActionRequestAction_AiClick,
    RunSdkActionRequestAction_AiInputText,
    RunSdkActionRequestAction_AiSelectOption,
    RunSdkActionRequestAction_AiUploadFile,
    RunSdkActionRequestAction_Extract,
)
from skyvern.config import settings
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi

if TYPE_CHECKING:
    from skyvern.library.skyvern_browser import SkyvernBrowser

LOG = structlog.get_logger()


class SdkSkyvernPageAi(SkyvernPageAi):
    """Implementation of SkyvernPageAi that makes API calls to the server."""

    def __init__(
        self,
        browser: "SkyvernBrowser",
        page: Page,
    ):
        self._browser = browser
        self._page = page

    async def ai_click(
        self,
        selector: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str | None:
        """Click an element using AI via API call."""

        LOG.info("AI click", intention=intention, workflow_run_id=self._browser.workflow_run_id)

        response = await self._browser.skyvern.run_sdk_action(
            url=self._page.url,
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
            action=RunSdkActionRequestAction_AiClick(
                selector=selector,
                intention=intention,
                data=data,
                timeout=timeout,
            ),
        )
        self._browser.workflow_run_id = response.workflow_run_id
        return response.result if response.result else selector

    async def ai_input_text(
        self,
        selector: str | None,
        value: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Input text into an element using AI via API call."""

        LOG.info("AI input text", intention=intention, workflow_run_id=self._browser.workflow_run_id)

        response = await self._browser.skyvern.run_sdk_action(
            url=self._page.url,
            action=RunSdkActionRequestAction_AiInputText(
                selector=selector,
                value=value,
                intention=intention,
                data=data,
                totp_identifier=totp_identifier,
                totp_url=totp_url,
                timeout=timeout,
            ),
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
        )
        self._browser.workflow_run_id = response.workflow_run_id
        return response.result if response.result else value or ""

    async def ai_select_option(
        self,
        selector: str | None,
        value: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Select an option from a dropdown using AI via API call."""

        LOG.info("AI select option", intention=intention, workflow_run_id=self._browser.workflow_run_id)

        response = await self._browser.skyvern.run_sdk_action(
            url=self._page.url,
            action=RunSdkActionRequestAction_AiSelectOption(
                selector=selector,
                value=value,
                intention=intention,
                data=data,
                timeout=timeout,
            ),
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
        )
        self._browser.workflow_run_id = response.workflow_run_id
        return response.result if response.result else value or ""

    async def ai_upload_file(
        self,
        selector: str | None,
        files: str | None,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        public_url_only: bool = False,
    ) -> str:
        """Upload a file using AI via API call."""

        LOG.info("AI upload file", intention=intention, workflow_run_id=self._browser.workflow_run_id)

        response = await self._browser.skyvern.run_sdk_action(
            url=self._page.url,
            action=RunSdkActionRequestAction_AiUploadFile(
                selector=selector,
                file_url=files,
                intention=intention,
                data=data,
                timeout=timeout,
            ),
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
        )
        self._browser.workflow_run_id = response.workflow_run_id
        return response.result if response.result else files or ""

    async def ai_extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        """Extract information from the page using AI via API call."""

        LOG.info("AI extract", prompt=prompt, workflow_run_id=self._browser.workflow_run_id)

        response = await self._browser.skyvern.run_sdk_action(
            url=self._page.url,
            action=RunSdkActionRequestAction_Extract(
                prompt=prompt,
                extract_schema=schema,
                error_code_mapping=error_code_mapping,
                intention=intention,
                data=data,
            ),
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
        )
        self._browser.workflow_run_id = response.workflow_run_id
        return response.result if response.result else None

    async def ai_act(
        self,
        prompt: str,
    ) -> None:
        """Perform an action on the page using AI via API call."""

        LOG.info("AI act", prompt=prompt, workflow_run_id=self._browser.workflow_run_id)

        response = await self._browser.skyvern.run_sdk_action(
            url=self._page.url,
            action=RunSdkActionRequestAction_AiAct(
                intention=prompt,
            ),
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
        )
        self._browser.workflow_run_id = response.workflow_run_id
