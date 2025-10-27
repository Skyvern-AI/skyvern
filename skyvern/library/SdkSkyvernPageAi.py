from typing import TYPE_CHECKING, Any

from playwright.async_api import Page

from skyvern.config import settings
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi
from skyvern.forge.sdk.schemas.sdk_actions import (
    ClickAction,
    ExtractAction,
    InputTextAction,
    SelectOptionAction,
)

if TYPE_CHECKING:
    from skyvern.library.skyvern_browser import SkyvernBrowser


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
        selector: str,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Click an element using AI via API call."""

        action = ClickAction(
            selector=selector,
            intention=intention,
            data=data,
            timeout=timeout,
        )
        response = await self._browser.client.run_sdk_action(
            url=self._page.url,
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
            action=action,
        )
        self._browser.workflow_run_id = response.workflow_run_id
        return response.result if response.result else selector

    async def ai_input_text(
        self,
        selector: str,
        value: str,
        intention: str,
        data: str | dict[str, Any] | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Input text into an element using AI via API call."""

        response = await self._browser.client.run_sdk_action(
            url=self._page.url,
            action=InputTextAction(
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
        return response.result if response.result else value

    async def ai_select_option(
        self,
        selector: str,
        value: str,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Select an option from a dropdown using AI via API call."""

        response = await self._browser.client.run_sdk_action(
            url=self._page.url,
            action=SelectOptionAction(
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
        return response.result if response.result else value

    async def ai_upload_file(
        self,
        selector: str,
        files: str,
        intention: str,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        raise NotImplementedError("Upload is not supported yet")

    async def ai_extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        """Extract information from the page using AI via API call."""

        response = await self._browser.client.run_sdk_action(
            url=self._page.url,
            action=ExtractAction(
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
