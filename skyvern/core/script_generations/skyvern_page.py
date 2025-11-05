from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Literal

import structlog
from playwright.async_api import Page

from skyvern.config import settings
from skyvern.core.script_generations.real_skyvern_page_ai import render_template
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi
from skyvern.forge.sdk.api.files import download_file
from skyvern.forge.sdk.core import skyvern_context
from skyvern.utils.url_validators import prepend_scheme_and_validate_url
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.action_types import ActionType

LOG = structlog.get_logger()


class Driver(StrEnum):
    PLAYWRIGHT = "playwright"


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


class SkyvernPage:
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
        self.page = page
        self.current_label: str | None = None
        self._ai = ai

    async def _decorate_call(
        self,
        fn: Callable,
        action: ActionType,
        *args: Any,
        prompt: str = "",
        data: str | dict[str, Any] = "",
        intention: str = "",  # backward compatibility
        **kwargs: Any,
    ) -> Any:
        return await fn(self, *args, prompt=prompt, data=data, intention=intention, **kwargs)

    @staticmethod
    def action_wrap(
        action: ActionType,
    ) -> Callable:
        def decorator(fn: Callable) -> Callable:
            async def wrapper(
                skyvern_page: SkyvernPage,
                *args: Any,
                prompt: str = "",
                data: str | dict[str, Any] = "",
                intention: str = "",  # backward compatibility
                **kwargs: Any,
            ) -> Any:
                return await skyvern_page._decorate_call(
                    fn, action, *args, prompt=prompt, data=data, intention=intention, **kwargs
                )

            return wrapper

        return decorator

    async def goto(self, url: str, timeout: float = settings.BROWSER_LOADING_TIMEOUT_MS) -> None:
        url = render_template(url)
        url = prepend_scheme_and_validate_url(url)

        # Print navigation in script mode
        context = skyvern_context.current()
        if context and context.script_mode:
            print(f"🌐 Navigating to: {url}")

        await self.page.goto(
            url,
            timeout=timeout,
        )

        if context and context.script_mode:
            print("  ✓ Page loaded")

    ######### Public Interfaces #########
    @action_wrap(ActionType.CLICK)
    async def click(
        self,
        selector: str,
        prompt: str | None = None,
        ai: str | None = "fallback",
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        intention: str | None = None,  # backward compatibility
    ) -> str:
        """Click an element identified by ``selector``.

        When ``prompt`` and ``data`` are provided a new click action is
        generated via the ``single-click-action`` prompt.  The model returns a
        fresh "xpath=..." selector based on the current DOM and the updated data for this run.
        The browser then clicks the element using this newly generated xpath selector.

        If the prompt generation or parsing fails for any reason we fall back to
        clicking the originally supplied ``selector``.
        """
        # Backward compatibility
        if intention is not None and prompt is None:
            prompt = intention

        context = skyvern_context.current()
        if context and context.ai_mode_override:
            ai = context.ai_mode_override
        if ai == "fallback":
            # try to click the element with the original selector first
            error_to_raise = None
            try:
                locator = self.page.locator(selector)
                await locator.click(timeout=timeout)
                return selector
            except Exception as e:
                error_to_raise = e

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
        locator = self.page.locator(selector)
        await locator.click(timeout=timeout)
        return selector

    @action_wrap(ActionType.INPUT_TEXT)
    async def fill(
        self,
        selector: str | None,
        value: str,
        ai: str | None = "fallback",
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        intention: str | None = None,  # backward compatibility
    ) -> str:
        # Backward compatibility
        if intention is not None and prompt is None:
            prompt = intention

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

    @action_wrap(ActionType.INPUT_TEXT)
    async def type(
        self,
        selector: str | None,
        value: str,
        ai: str | None = "fallback",
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
        intention: str | None = None,  # backward compatibility
    ) -> str:
        # Backward compatibility
        if intention is not None and prompt is None:
            prompt = intention

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

        When ``prompt`` and ``data`` are provided a new input text action is
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

    @action_wrap(ActionType.UPLOAD_FILE)
    async def upload_file(
        self,
        selector: str | None,
        files: str,
        ai: str | None = "fallback",
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        intention: str | None = None,  # backward compatibility
    ) -> str:
        # Backward compatibility
        if intention is not None and prompt is None:
            prompt = intention

        context = skyvern_context.current()
        if context and context.ai_mode_override:
            ai = context.ai_mode_override
        if ai == "fallback":
            error_to_raise = None
            if selector:
                try:
                    file_path = await download_file(files)
                    locator = self.page.locator(selector)
                    await locator.set_input_files(file_path)
                except Exception as e:
                    error_to_raise = e

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

        file_path = await download_file(files)
        locator = self.page.locator(selector)
        await locator.set_input_files(file_path, timeout=timeout)
        return files

    @action_wrap(ActionType.SELECT_OPTION)
    async def select_option(
        self,
        selector: str,
        value: str | None = None,
        label: str | None = None,
        ai: str | None = "fallback",
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        intention: str | None = None,  # backward compatibility
    ) -> str:
        # Backward compatibility
        if intention is not None and prompt is None:
            prompt = intention

        context = skyvern_context.current()
        if context and context.ai_mode_override:
            ai = context.ai_mode_override
        value = value or ""
        if ai == "fallback":
            error_to_raise = None
            try:
                locator = self.page.locator(selector)
                await locator.select_option(value, timeout=timeout)
                return value
            except Exception as e:
                error_to_raise = e
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
        locator = self.page.locator(selector)
        await locator.select_option(value, timeout=timeout)
        return value

    @action_wrap(ActionType.WAIT)
    async def wait(
        self,
        seconds: float,
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        intention: str | None = None,
    ) -> None:
        await asyncio.sleep(seconds)

    @action_wrap(ActionType.NULL_ACTION)
    async def null_action(
        self, prompt: str | None = None, data: str | dict[str, Any] | None = None, intention: str | None = None
    ) -> None:
        return

    @action_wrap(ActionType.SOLVE_CAPTCHA)
    async def solve_captcha(
        self, prompt: str | None = None, data: str | dict[str, Any] | None = None, intention: str | None = None
    ) -> None:
        raise NotImplementedError("Solve captcha is not supported outside server context")

    @action_wrap(ActionType.TERMINATE)
    async def terminate(
        self,
        errors: list[str],
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        intention: str | None = None,
    ) -> None:
        # TODO: update the workflow run status to terminated
        return

    @action_wrap(ActionType.COMPLETE)
    async def complete(
        self, prompt: str | None = None, data: str | dict[str, Any] | None = None, intention: str | None = None
    ) -> None:
        """Stub for complete. Override in subclasses for specific behavior."""

    @action_wrap(ActionType.RELOAD_PAGE)
    async def reload_page(
        self, prompt: str | None = None, data: str | dict[str, Any] | None = None, intention: str | None = None
    ) -> None:
        await self.page.reload()
        return

    @action_wrap(ActionType.EXTRACT)
    async def extract(
        self,
        prompt: str,
        schema: dict[str, Any] | list | str | None = None,
        error_code_mapping: dict[str, str] | None = None,
        intention: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> dict[str, Any] | list | str | None:
        return await self._ai.ai_extract(prompt, schema, error_code_mapping, intention, data)

    @action_wrap(ActionType.VERIFICATION_CODE)
    async def verification_code(
        self, prompt: str | None = None, data: str | dict[str, Any] | None = None, intention: str | None = None
    ) -> None:
        return

    @action_wrap(ActionType.SCROLL)
    async def scroll(
        self,
        scroll_x: int,
        scroll_y: int,
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        intention: str | None = None,
    ) -> None:
        await self.page.evaluate(f"window.scrollBy({scroll_x}, {scroll_y})")

    @action_wrap(ActionType.KEYPRESS)
    async def keypress(
        self,
        keys: list[str],
        hold: bool = False,
        duration: float = 0,
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        intention: str | None = None,  # backward compatibility
    ) -> None:
        await handler_utils.keypress(self.page, keys, hold=hold, duration=duration)

    @action_wrap(ActionType.MOVE)
    async def move(
        self,
        x: int,
        y: int,
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        intention: str | None = None,
    ) -> None:
        await self.page.mouse.move(x, y)

    @action_wrap(ActionType.DRAG)
    async def drag(
        self,
        start_x: int,
        start_y: int,
        path: list[tuple[int, int]],
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        intention: str | None = None,  # backward compatibility
    ) -> None:
        await handler_utils.drag(self.page, start_x, start_y, path)

    @action_wrap(ActionType.LEFT_MOUSE)
    async def left_mouse(
        self,
        x: int,
        y: int,
        direction: Literal["down", "up"],
        prompt: str | None = None,
        data: str | dict[str, Any] | None = None,
        intention: str | None = None,  # backward compatibility
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
