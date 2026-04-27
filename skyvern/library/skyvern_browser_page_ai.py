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
    RunSdkActionRequestAction_LocateElement,
    RunSdkActionRequestAction_Prompt,
    RunSdkActionRequestAction_Validate,
)
from skyvern.config import settings
from skyvern.core.script_generations.skyvern_page_ai import SYSTEM_PROMPT_UNSET, SkyvernPageAi

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
        failed_selector: str | None = None,  # noqa: ARG002 — accepted for Protocol compat, no episode recording in library path
        block_label: str | None = None,  # noqa: ARG002
    ) -> str | None:
        """Click an element using AI via API call.

        Note: failed_selector/block_label are accepted for SkyvernPageAi Protocol
        compatibility but intentionally ignored. The library path (SDK/CLI) lacks
        the workflow context (code_version, workflow_run_id, DB) needed to create
        fallback episodes. Episode recording lives in real_skyvern_page_ai.py.
        """

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
        failed_selector: str | None = None,  # noqa: ARG002 — Protocol compat, see ai_click docstring
        block_label: str | None = None,  # noqa: ARG002
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
        skip_refresh: bool = False,
        include_extracted_text: bool = True,
        system_prompt: str | None | Any = SYSTEM_PROMPT_UNSET,
    ) -> dict[str, Any] | list | str | None:
        """Extract information from the page using AI via API call.

        Note: skip_refresh, include_extracted_text, and system_prompt are
        accepted for Protocol compatibility but not forwarded to the API. The
        server-side controls them via the Task record on the SDK HTTP path.
        The optimizations only take effect on the direct RealSkyvernPageAI
        path (MCP local browser).
        """

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

    async def ai_validate(
        self,
        prompt: str,
        model: dict[str, Any] | None = None,
    ) -> bool:
        """Validate the current page state using AI via API call."""

        LOG.info(
            "AI validate",
            prompt=prompt,
            model=model,
            workflow_run_id=self._browser.workflow_run_id,
        )

        response = await self._browser.skyvern.run_sdk_action(
            url=self._page.url,
            action=RunSdkActionRequestAction_Validate(
                prompt=prompt,
                model=model,
            ),
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
        )
        self._browser.workflow_run_id = response.workflow_run_id

        return bool(response.result) if response.result is not None else False

    async def ai_act(
        self,
        prompt: str,
        skip_refresh: bool = False,
        use_economy_tree: bool = False,
    ) -> None:
        """Perform an action on the page using AI via API call.

        Note: skip_refresh and use_economy_tree are accepted for Protocol compatibility
        but not forwarded to the API. The optimizations only take effect on the direct
        RealSkyvernPageAI path (MCP local browser).
        """

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

    async def ai_locate_element(
        self,
        prompt: str,
    ) -> str | None:
        """Locate an element on the page using AI and return its XPath selector via API call.

        Args:
            prompt: Natural language description of the element to locate (e.g., 'find "download invoices" button')

        Returns:
            XPath selector string (e.g., 'xpath=//button[@id="download"]') or None if not found
        """

        LOG.info("AI locate element", prompt=prompt, workflow_run_id=self._browser.workflow_run_id)

        response = await self._browser.skyvern.run_sdk_action(
            url=self._page.url,
            action=RunSdkActionRequestAction_LocateElement(
                prompt=prompt,
            ),
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
        )
        self._browser.workflow_run_id = response.workflow_run_id

        # Return the XPath result directly
        if response.result and isinstance(response.result, str):
            return response.result

        return None

    async def ai_prompt(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        model: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        """Send a prompt to the LLM and get a response based on the provided schema via API call."""

        LOG.info(
            "AI prompt",
            prompt=prompt,
            model=model,
            workflow_run_id=self._browser.workflow_run_id,
        )

        response = await self._browser.skyvern.run_sdk_action(
            url=self._page.url,
            action=RunSdkActionRequestAction_Prompt(
                prompt=prompt,
                response_schema=schema,
                model=model,
            ),
            browser_session_id=self._browser.browser_session_id,
            browser_address=self._browser.browser_address,
            workflow_run_id=self._browser.workflow_run_id,
        )
        self._browser.workflow_run_id = response.workflow_run_id

        return response.result if response.result is not None else None

    async def ai_classify(
        self,
        options: dict[str, str],
        url_patterns: dict[str, str] | None = None,
        text_patterns: dict[str, str | list[str]] | None = None,
    ) -> str:
        """Classify the current page state against named options.

        Not yet supported via the SDK API. Raises NotImplementedError.
        """
        raise NotImplementedError("ai_classify is not yet supported via the SDK API")

    async def ai_element_fallback(
        self,
        navigation_goal: str,
        max_steps: int = 10,
    ) -> None:
        """Activate the AI agent from the current page position.

        Not yet supported via the SDK API. Raises NotImplementedError.
        """
        raise NotImplementedError("ai_element_fallback is not yet supported via the SDK API")
