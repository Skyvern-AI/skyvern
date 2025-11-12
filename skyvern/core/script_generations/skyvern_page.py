from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import Any, Callable, Literal, overload

import structlog
from playwright.async_api import Page

from skyvern.config import settings
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi
from skyvern.forge.sdk.api.files import download_file
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.action_types import ActionType

LOG = structlog.get_logger()


@dataclass
class ActionMetadata:
    prompt: str = ""
    data: dict[str, Any] | str | None = None
    timestamp: float | None = None  # filled in by recorder
    screenshot_path: str | None = None  # if enabled


@dataclass
class ActionCall:
    name: ActionType
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    meta: ActionMetadata
    result: Any | None = None  # populated after execution
    error: Exception | None = None  # populated if failed


class SkyvernPage(Page):
    """
    A lightweight adapter for the selected driver that:
    1. Executes actual browser commands
    2. Enables AI-driven actions
    3. Provides an AI-based fallback for standard actions
    """

    def __init__(
        self,
        page: Page,
        ai: SkyvernPageAi,
    ) -> None:
        super().__init__(page)
        self.page = page
        self.current_label: str | None = None
        self._ai = ai

    def __getattribute__(self, name: str) -> Any:
        page = object.__getattribute__(self, "page")
        if hasattr(page, name):
            for cls in type(self).__mro__:
                if cls is Page:
                    break
                if name in cls.__dict__:
                    return object.__getattribute__(self, name)
            return getattr(page, name)

        return object.__getattribute__(self, name)

    async def _decorate_call(
        self,
        fn: Callable,
        action: ActionType,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return await fn(self, *args, **kwargs)

    @staticmethod
    def action_wrap(
        action: ActionType,
    ) -> Callable:
        def decorator(fn: Callable) -> Callable:
            async def wrapper(
                skyvern_page: SkyvernPage,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                return await skyvern_page._decorate_call(fn, action, *args, **kwargs)

            return wrapper

        return decorator

    async def goto(self, url: str, **kwargs: Any) -> None:
        timeout = kwargs.pop("timeout", settings.BROWSER_ACTION_TIMEOUT_MS)
        await self.page.goto(url, timeout=timeout, **kwargs)

    ######### Public Interfaces #########

    @overload
    async def click(
        self,
        selector: str,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> str | None: ...

    @overload
    async def click(
        self,
        *,
        prompt: str,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> str | None: ...

    @action_wrap(ActionType.CLICK)
    async def click(
        self,
        selector: str | None = None,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
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
            **kwargs: All Playwright click parameters (timeout, force, modifiers, etc.)

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
        # Backward compatibility
        intention = kwargs.pop("intention", None)
        if intention is not None and prompt is None:
            prompt = intention

        if not selector and not prompt:
            raise ValueError("Missing input: pass a selector and/or a prompt.")

        timeout = kwargs.pop("timeout", settings.BROWSER_ACTION_TIMEOUT_MS)
        data = kwargs.pop("data", None)

        context = skyvern_context.current()
        if context and context.ai_mode_override:
            ai = context.ai_mode_override
        if ai == "fallback":
            # try to click the element with the original selector first
            error_to_raise = None
            if selector:
                try:
                    locator = self.page.locator(selector)
                    await locator.click(timeout=timeout, **kwargs)
                    return selector
                except Exception as e:
                    error_to_raise = e
                    selector = None

            # if the original selector doesn't work, try to click the element with the ai generated selector
            if prompt:
                return await self._ai.ai_click(
                    selector=selector,
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
                    selector=selector,
                    intention=prompt,
                    data=data,
                    timeout=timeout,
                )

        if selector:
            locator = self.page.locator(selector)
            await locator.click(timeout=timeout, **kwargs)

        return selector

    @overload
    async def fill(
        self,
        selector: str,
        value: str,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        **kwargs: Any,
    ) -> str: ...

    @overload
    async def fill(
        self,
        *,
        prompt: str,
        value: str | None = None,
        selector: str | None = None,
        ai: str | None = "fallback",
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        **kwargs: Any,
    ) -> str: ...

    @action_wrap(ActionType.INPUT_TEXT)
    async def fill(
        self,
        selector: str | None = None,
        value: str | None = None,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Fill an input field using a CSS selector, AI-powered prompt matching, or both.

        This method supports three modes:
        - **Selector-based**: Fill the input field with a value using CSS selector
        - **AI-powered**: Use natural language prompt (AI extracts value from prompt)
        - **Fallback mode** (default): Try the selector first, fall back to AI if it fails

        Args:
            selector: CSS selector for the target input element.
            value: The text value to input into the field.
            prompt: Natural language description of which field to fill and what value.
            ai: AI behavior mode. Defaults to "fallback" which tries selector first, then AI.
            totp_identifier: TOTP identifier for time-based one-time password fields.
            totp_url: URL to fetch TOTP codes from for authentication.

        Returns:
            The value that was successfully filled into the field.

        Examples:
            ```python
            # Fill using selector and value (both positional)
            await page.fill("#email-input", "user@example.com")

            # Fill using AI with natural language (prompt only)
            await page.fill(prompt="Fill 'user@example.com' in the email address field")

            # Try selector first, fall back to AI if selector fails
            await page.fill(
                "#email-input",
                "user@example.com",
                prompt="Fill the email address with user@example.com"
            )
            ```
        """

        # Backward compatibility
        intention = kwargs.pop("intention", None)
        if intention is not None and prompt is None:
            prompt = intention

        if not selector and not prompt:
            raise ValueError("Missing input: pass a selector and/or a prompt.")

        timeout = kwargs.pop("timeout", settings.BROWSER_ACTION_TIMEOUT_MS)
        data = kwargs.pop("data", None)

        return await self._input_text(
            selector=selector,
            value=value or "",
            ai=ai,
            intention=prompt,
            data=data,
            timeout=timeout,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
        )

    @action_wrap(ActionType.INPUT_TEXT)
    async def type(
        self,
        selector: str | None,
        value: str,
        ai: str | None = "fallback",
        prompt: str | None = None,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        **kwargs: Any,
    ) -> str:
        # Backward compatibility
        intention = kwargs.pop("intention", None)
        if intention is not None and prompt is None:
            prompt = intention

        if not selector and not prompt:
            raise ValueError("Missing input: pass a selector and/or a prompt.")

        timeout = kwargs.pop("timeout", settings.BROWSER_ACTION_TIMEOUT_MS)
        data = kwargs.pop("data", None)

        return await self._input_text(
            selector=selector,
            value=value,
            ai=ai,
            intention=prompt,
            data=data,
            timeout=timeout,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
        )

    async def _input_text(
        self,
        selector: str | None,
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
        inputting the originally supplied ``text``.
        """

        context = skyvern_context.current()
        if context and context.ai_mode_override:
            ai = context.ai_mode_override

        # format the text with the actual value of the parameter if it's a secret when running a workflow
        if ai == "fallback":
            error_to_raise = None
            if selector:
                try:
                    locator = self.page.locator(selector)
                    await handler_utils.input_sequentially(locator, value, timeout=timeout)
                    return value
                except Exception as e:
                    error_to_raise = e
                    selector = None

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

        if not selector:
            raise ValueError("Selector is required but was not provided")

        locator = self.page.locator(selector)
        await handler_utils.input_sequentially(locator, value, timeout=timeout)
        return value

    @overload
    async def upload_file(
        self,
        selector: str,
        files: str,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> str: ...

    @overload
    async def upload_file(
        self,
        *,
        prompt: str,
        files: str | None = None,
        selector: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> str: ...

    @action_wrap(ActionType.UPLOAD_FILE)
    async def upload_file(
        self,
        selector: str | None = None,
        files: str | None = None,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> str:
        # Backward compatibility
        intention = kwargs.pop("intention", None)
        if intention is not None and prompt is None:
            prompt = intention

        if not selector and not prompt:
            raise ValueError("Missing input: pass a selector and/or a prompt.")

        timeout = kwargs.pop("timeout", settings.BROWSER_ACTION_TIMEOUT_MS)
        data = kwargs.pop("data", None)

        context = skyvern_context.current()
        if context and context.ai_mode_override:
            ai = context.ai_mode_override
        if ai == "fallback":
            if not files and not prompt:
                raise ValueError("Missing input: files should be provided explicitly or in prompt")

            error_to_raise = None
            if selector and files:
                try:
                    file_path = await download_file(files)
                    locator = self.page.locator(selector)
                    await locator.set_input_files(file_path, **kwargs)
                except Exception as e:
                    error_to_raise = e
                    selector = None

            if prompt:
                return await self._ai.ai_upload_file(
                    selector=selector,
                    files=files,
                    intention=prompt,
                    data=data,
                    timeout=timeout,
                )
            if error_to_raise:
                raise error_to_raise
            elif not files:
                raise ValueError("Parameter 'files' is required but was not provided")
            else:
                return files
        elif ai == "proactive" and prompt:
            return await self._ai.ai_upload_file(
                selector=selector,
                files=files,
                intention=prompt,
                data=data,
                timeout=timeout,
            )

        if not selector:
            raise ValueError("Selector is required but was not provided")
        if not files:
            raise ValueError("Parameter 'files' is required but was not provided")

        file_path = await download_file(files)
        locator = self.page.locator(selector)
        await locator.set_input_files(file_path, timeout=timeout, **kwargs)
        return files

    @overload
    async def select_option(
        self,
        selector: str,
        value: str | None = None,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> str | None: ...

    @overload
    async def select_option(
        self,
        *,
        prompt: str,
        value: str | None = None,
        selector: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> str | None: ...

    @action_wrap(ActionType.SELECT_OPTION)
    async def select_option(
        self,
        selector: str | None = None,
        value: str | None = None,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> str | None:
        """Select an option from a dropdown using a CSS selector, AI-powered prompt matching, or both.

        This method supports three modes:
        - **Selector-based**: Select the option with a value using CSS selector
        - **AI-powered**: Use natural language prompt (AI extracts value from prompt)
        - **Fallback mode** (default): Try the selector first, fall back to AI if it fails

        Args:
            selector: CSS selector for the target select/dropdown element.
            value: The option value to select.
            prompt: Natural language description of which option to select.
            ai: AI behavior mode. Defaults to "fallback" which tries selector first, then AI.

        Returns:
            The value that was successfully selected.

        Examples:
            ```python
            # Select using selector and value (both positional)
            await page.select_option("#country", "us")

            # Select using AI with natural language (prompt only)
            await page.select_option(prompt="Select 'United States' from the country dropdown")

            # Try selector first, fall back to AI if selector fails
            await page.select_option(
                "#country",
                "us",
                prompt="Select United States from country"
            )
            ```
        """

        # Backward compatibility
        intention = kwargs.pop("intention", None)
        if intention is not None and prompt is None:
            prompt = intention

        if not selector and not prompt:
            raise ValueError("Missing input: pass a selector and/or a prompt.")

        timeout = kwargs.pop("timeout", settings.BROWSER_ACTION_TIMEOUT_MS)
        data = kwargs.pop("data", None)

        context = skyvern_context.current()
        if context and context.ai_mode_override:
            ai = context.ai_mode_override
        value = value or ""
        if ai == "fallback":
            error_to_raise = None
            if selector:
                try:
                    locator = self.page.locator(selector)
                    await locator.select_option(value, timeout=timeout, **kwargs)
                    return value
                except Exception as e:
                    error_to_raise = e
                    selector = None

            if prompt:
                return await self._ai.ai_select_option(
                    selector=selector,
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
                selector=selector,
                value=value,
                intention=prompt,
                data=data,
                timeout=timeout,
            )
        if selector:
            locator = self.page.locator(selector)
            await locator.select_option(value, timeout=timeout, **kwargs)
        return value

    @action_wrap(ActionType.WAIT)
    async def wait(
        self,
        seconds: float,
        **kwargs: Any,
    ) -> None:
        await asyncio.sleep(seconds)

    @action_wrap(ActionType.NULL_ACTION)
    async def null_action(self, **kwargs: Any) -> None:
        return

    @action_wrap(ActionType.SOLVE_CAPTCHA)
    async def solve_captcha(self, prompt: str | None = None) -> None:
        raise NotImplementedError("Solve captcha is not supported outside server context")

    @action_wrap(ActionType.TERMINATE)
    async def terminate(self, errors: list[str], **kwargs: Any) -> None:
        # TODO: update the workflow run status to terminated
        return

    @action_wrap(ActionType.COMPLETE)
    async def complete(self, prompt: str | None = None) -> None:
        """Stub for complete. Override in subclasses for specific behavior."""

    @action_wrap(ActionType.RELOAD_PAGE)
    async def reload_page(self, **kwargs: Any) -> None:
        await self.page.reload(**kwargs)
        return

    @action_wrap(ActionType.EXTRACT)
    async def extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | list | str | None:
        """Extract structured data from the page using AI.

        Args:
            prompt: Natural language description of what data to extract.
            schema: JSON Schema defining the structure of data to extract.
            error_code_mapping: Mapping of error codes to custom error messages.
            intention: Additional context about the extraction intent.

        Returns:
            Extracted data matching the provided schema, or None if extraction fails.

        Examples:
            ```python
            # Extract structured data with JSON Schema
            result = await page.extract(
                prompt="Extract product information",
                schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Product name"},
                        "price": {"type": "number", "description": "Product price"}
                    },
                    "required": ["name", "price"]
                }
            )
            # Returns: {"name": "...", "price": 29.99}
            ```
        """
        data = kwargs.pop("data", None)
        return await self._ai.ai_extract(prompt, schema, error_code_mapping, intention, data)

    @action_wrap(ActionType.VERIFICATION_CODE)
    async def verification_code(self, prompt: str | None = None) -> None:
        return

    @action_wrap(ActionType.SCROLL)
    async def scroll(
        self,
        scroll_x: int,
        scroll_y: int,
        **kwargs: Any,
    ) -> None:
        await self.page.evaluate(f"window.scrollBy({scroll_x}, {scroll_y})")

    @action_wrap(ActionType.KEYPRESS)
    async def keypress(
        self,
        keys: list[str],
        hold: bool = False,
        duration: float = 0,
        **kwargs: Any,
    ) -> None:
        await handler_utils.keypress(self.page, keys, hold=hold, duration=duration)

    @action_wrap(ActionType.MOVE)
    async def move(
        self,
        x: int,
        y: int,
        **kwargs: Any,
    ) -> None:
        await self.page.mouse.move(x, y)

    @action_wrap(ActionType.DRAG)
    async def drag(
        self,
        start_x: int,
        start_y: int,
        path: list[tuple[int, int]],
        **kwargs: Any,
    ) -> None:
        await handler_utils.drag(self.page, start_x, start_y, path)

    @action_wrap(ActionType.LEFT_MOUSE)
    async def left_mouse(
        self,
        x: int,
        y: int,
        direction: Literal["down", "up"],
        **kwargs: Any,
    ) -> None:
        await handler_utils.left_mouse(self.page, x, y, direction)


class RunContext:
    def __init__(
        self, parameters: dict[str, Any], page: SkyvernPage, generated_parameters: dict[str, Any] | None = None
    ) -> None:
        self.original_parameters = parameters
        self.generated_parameters = generated_parameters
        self.parameters = copy.deepcopy(parameters)
        if generated_parameters:
            # hydrate the generated parameter fields in the run context parameters
            for key, value in generated_parameters.items():
                if key not in self.parameters:
                    self.parameters[key] = value
        self.page = page
        self.trace: list[ActionCall] = []
