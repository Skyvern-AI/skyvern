from __future__ import annotations

import asyncio
import copy
import os
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal, overload

import structlog
from playwright.async_api import Locator, Page

from skyvern.config import settings
from skyvern.core.script_generations.canonical_fields import get_category, match_field_to_category
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import download_file as download_file_from_url
from skyvern.forge.sdk.core import skyvern_context
from skyvern.library.ai_locator import AILocator
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.action_types import ActionType

if TYPE_CHECKING:
    from skyvern.webeye.actions.actions import Action
    from skyvern.webeye.actions.responses import ActionResult

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
        context = skyvern_context.current()
        # label = self.current_label
        # action_override = None
        # if context and label:
        #     current_count = context.action_counters.get(label, 0) + 1
        # context.action_counters[label] = current_count
        # action_override = context.action_ai_overrides.get(label, {}).get(current_count)
        # context.ai_mode_override = action_override

        try:
            return await fn(self, *args, **kwargs)
        finally:
            if context:
                # Reset override after each action so defaults apply when no mapping is provided.
                # context.ai_mode_override = None
                pass

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
        timeout = kwargs.pop("timeout", settings.BROWSER_LOADING_TIMEOUT_MS)
        await self.page.goto(url, timeout=timeout, **kwargs)

    async def get_actual_value(
        self,
        value: str,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> str:
        return value

    async def get_totp_digit(
        self,
        context: Any,
        field_name: str,
        digit_index: int,
        totp_identifier: str | None = None,
        totp_url: str | None = None,
    ) -> str:
        """
        Get a specific digit from a TOTP code for multi-field TOTP inputs.

        This method is used by generated scripts for multi-field TOTP where each
        input field needs a single digit. It resolves the full TOTP code from
        the credential and returns the specific digit.

        Args:
            context: The run context containing parameters
            field_name: The parameter name containing the TOTP code or credential reference
            digit_index: The index of the digit to return (0-5 for a 6-digit TOTP)
            totp_identifier: Optional TOTP identifier for polling
            totp_url: Optional TOTP verification URL

        Returns:
            The single digit at the specified index
        """
        # Get the raw parameter value (may be credential reference like BW_TOTP)
        raw_value = context.parameters.get(field_name, "")
        # Resolve the actual TOTP code (this handles credential generation)
        totp_code = await self.get_actual_value(raw_value, totp_identifier, totp_url)
        # Return the specific digit
        if digit_index < len(totp_code):
            return totp_code[digit_index]
        return ""

    @staticmethod
    def _track_ai_call() -> None:
        """Increment the script LLM call counter for cost-cap tracking."""
        ctx = skyvern_context.current()
        if ctx:
            ctx.script_llm_call_count += 1

    async def _prepare_element(self, locator: Any, timeout: float = 5000) -> None:
        """Prepare an element for interaction, matching agent-level robustness.

        The agent handler does scroll_into_view, visibility checks, and animation
        waits before every click/fill.  Scripts historically skipped all of this,
        causing failures on elements that are off-screen, still animating, or
        covered by overlays.  This method closes that gap.
        """
        try:
            await locator.wait_for(state="visible", timeout=timeout)
        except Exception:
            pass  # element may already be visible; don't block on timeout
        try:
            await locator.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            pass  # best-effort — some elements can't be scrolled
        # Brief pause for CSS transitions / JS animations to settle.
        # The agent uses safe_wait_for_animation_end(); this is a lighter
        # equivalent that avoids importing heavy agent internals.
        await asyncio.sleep(0.15)

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
                    locator = self.page.locator(selector).first
                    await self._prepare_element(locator, timeout=timeout)
                    await locator.click(timeout=timeout, **kwargs)
                    return selector
                except Exception as e:
                    # The click may have failed because an autocomplete dropdown
                    # or other overlay is covering the target element.  Press
                    # Escape to dismiss it and retry once before falling to AI.
                    try:
                        await self.page.keyboard.press("Escape")
                        await asyncio.sleep(0.3)
                        locator = self.page.locator(selector).first
                        await locator.click(timeout=timeout, **kwargs)
                        LOG.info(
                            "CSS selector click succeeded after dismissing overlay",
                            selector=selector,
                        )
                        return selector
                    except Exception:
                        pass  # retry failed too — fall through to AI
                    LOG.warning(
                        "CSS selector click failed, falling back to AI",
                        selector=selector,
                        error=str(e),
                    )
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

    @action_wrap(ActionType.HOVER)
    async def hover(
        self,
        selector: str,
        *,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
        hold_seconds: float = 0.0,
        intention: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Move the mouse over the element identified by `selector`."""
        if not selector:
            raise ValueError("Hover requires a selector.")

        locator = self.page.locator(selector, **kwargs)
        await locator.scroll_into_view_if_needed()
        await locator.hover(timeout=timeout)
        if hold_seconds and hold_seconds > 0:
            await asyncio.sleep(hold_seconds)
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

        # Skip fill when value is None (missing parameter) and AI won't generate one.
        # ai='proactive' means the LLM generates the value from the prompt, so None is fine there.
        if value is None and ai != "proactive":
            LOG.info("Skipping fill — value is None (missing parameter)", selector=selector, prompt=prompt)
            return ""

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

    @action_wrap(ActionType.INPUT_TEXT)
    async def fill_autocomplete(
        self,
        selector: str | None = None,
        value: str | None = None,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        option_selector: str | None = None,
        wait_seconds: float = 1.5,
        **kwargs: Any,
    ) -> str:
        """Fill an autocomplete input by typing a value and clicking the matching dropdown option.

        Handles widgets like Google Places, Lever location fields, and other
        autocomplete inputs where typing triggers a dropdown and the user must
        select an option for the value to persist.

        The flow:
        1. Clear the field and type the value character-by-character (triggers autocomplete)
        2. Wait for dropdown options to appear
        3. Find the best-matching option by text similarity
        4. Click it so the value is committed

        If no dropdown options appear, falls back to the same behavior as ``page.fill()``.

        Args:
            selector: CSS selector for the input field.
            value: The text to type (e.g. "San Francisco, CA").
            prompt: Natural language description for AI fallback.
            ai: AI behavior mode (same as fill). Defaults to "fallback".
            option_selector: CSS selector for the dropdown options. If not provided,
                tries common patterns: ``[role="option"]``, ``.pac-item``, ``li[role="option"]``,
                ``[data-option-id]``.
            wait_seconds: How long to wait for dropdown options to appear after typing.
                Defaults to 1.5 seconds.

        Returns:
            The value that was selected from the dropdown, or the typed value if no dropdown appeared.

        Examples:
            ```python
            # Autocomplete with known value
            await page.fill_autocomplete(
                selector='label:has-text("Current location") input',
                value=context.parameters['current_location'],
                ai='fallback',
                prompt='Fill the current location of the applicant',
            )

            # Autocomplete where AI generates the value
            await page.fill_autocomplete(
                selector='label:has-text("City") input',
                ai='proactive',
                prompt='Fill the city where the applicant is based',
            )
            ```
        """
        # Backward compatibility
        intention = kwargs.pop("intention", None)
        if intention is not None and prompt is None:
            prompt = intention

        if not selector and not prompt:
            raise ValueError("Missing input: pass a selector and/or a prompt.")

        # Skip when value is None and AI won't generate one
        if value is None and ai != "proactive":
            LOG.info("Skipping fill_autocomplete — value is None (missing parameter)", selector=selector, prompt=prompt)
            return ""

        context = skyvern_context.current()
        if context and context.ai_mode_override:
            ai = context.ai_mode_override

        timeout = kwargs.pop("timeout", settings.BROWSER_ACTION_TIMEOUT_MS)
        data = kwargs.pop("data", None)

        # For proactive mode, delegate entirely to the AI — it knows how to handle
        # autocomplete via the agent's full action handler.
        if ai == "proactive" and prompt:
            return await self._ai.ai_input_text(
                selector=selector,
                value=value or "",
                intention=prompt,
                data=data,
                timeout=timeout,
            )

        # --- Selector-based autocomplete flow ---
        if not selector:
            # No selector, fall through to AI fallback below
            if prompt:
                return await self._ai.ai_input_text(
                    selector=None,
                    value=value or "",
                    intention=prompt,
                    data=data,
                    timeout=timeout,
                )
            raise ValueError("Selector is required but was not provided")

        actual_value = value or ""
        try:
            actual_value = await self.get_actual_value(
                actual_value,
                totp_identifier=kwargs.get("totp_identifier"),
                totp_url=kwargs.get("totp_url"),
            )
        except Exception:
            pass  # use original value

        try:
            result = await self._do_autocomplete(
                selector=selector,
                value=actual_value,
                option_selector=option_selector,
                wait_seconds=wait_seconds,
                timeout=timeout,
            )
            return result
        except Exception as e:
            LOG.info(
                "fill_autocomplete selector path failed, trying AI fallback",
                selector=selector,
                error=str(e),
            )
            if prompt:
                return await self._ai.ai_input_text(
                    selector=None,
                    value=actual_value,
                    intention=prompt,
                    data=data,
                    timeout=timeout,
                )
            raise

    # Common selectors for autocomplete dropdown options, tried in order.
    _AUTOCOMPLETE_OPTION_SELECTORS = [
        '[role="option"]:visible',
        ".pac-item:visible",  # Google Places
        '[role="listbox"] li:visible',
        "[data-option-id]:visible",
        "ul.autocomplete-results li:visible",
        '.dropdown-menu li:visible, .dropdown-menu [role="option"]:visible',
        ".autocomplete-dropdown-container div:visible",  # Lever location
        '[class*="suggestion"]:visible',
        '[class*="option"]:visible:not(select option)',
    ]

    async def _do_autocomplete(
        self,
        selector: str,
        value: str,
        option_selector: str | None = None,
        wait_seconds: float = 3.0,
        timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS,
    ) -> str:
        """Type into an autocomplete input and click the best matching dropdown option."""
        locator = self.page.locator(selector).first

        # Clear existing value and type character-by-character to trigger autocomplete
        await locator.clear(timeout=timeout)
        await handler_utils.input_sequentially(locator, value, timeout=timeout)

        # Poll for dropdown options to appear (check every 0.3s up to wait_seconds)
        option_locators: list[Locator] = []
        poll_interval = 0.3
        elapsed = 0.0
        while elapsed < wait_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            option_locators = await self._find_autocomplete_options(option_selector)
            if option_locators:
                break
        if not option_locators:
            # No dropdown appeared. The typed text may get cleared by strict
            # autocomplete widgets when focus leaves. Try pressing Enter to
            # commit, then verify the value stuck.
            LOG.info(
                "fill_autocomplete: no dropdown options found, trying Enter to commit",
                selector=selector,
                value=value,
            )
            await locator.press("Enter", timeout=timeout)
            await asyncio.sleep(0.5)

            # Check if the value persisted
            current_value = await locator.input_value(timeout=2000)
            if current_value.strip():
                LOG.info(
                    "fill_autocomplete: value committed via Enter",
                    selector=selector,
                    value=current_value,
                )
                return current_value

            # Value was cleared — strict autocomplete rejected freeform text.
            # Re-type and try clicking the first available option after a longer wait.
            LOG.info(
                "fill_autocomplete: value cleared after Enter, retrying with longer wait",
                selector=selector,
                value=value,
            )
            await locator.clear(timeout=timeout)
            await handler_utils.input_sequentially(locator, value, timeout=timeout)
            await asyncio.sleep(3.0)  # longer wait for slow API responses
            option_locators = await self._find_autocomplete_options(option_selector)
            if not option_locators:
                # Last resort: just fill the raw value and hope it sticks
                LOG.warning(
                    "fill_autocomplete: no dropdown after retry, filling raw value",
                    selector=selector,
                    value=value,
                )
                await locator.fill(value, timeout=timeout)
                return value

        # Find the best matching option by text similarity
        best_match = await self._find_best_option(option_locators, value)
        if best_match:
            await best_match.click(timeout=timeout)
            LOG.info(
                "fill_autocomplete: clicked matching dropdown option",
                selector=selector,
                value=value,
            )
            # Small delay for the value to commit
            await asyncio.sleep(0.3)
            return value
        else:
            # No close text match — click the first option as best guess
            first = option_locators[0]
            await first.click(timeout=timeout)
            LOG.info(
                "fill_autocomplete: no close text match, clicked first option",
                selector=selector,
                value=value,
            )
            await asyncio.sleep(0.3)
            return value

    async def _find_autocomplete_options(
        self,
        custom_selector: str | None = None,
    ) -> list[Locator]:
        """Find visible autocomplete dropdown options on the page."""
        selectors_to_try = [custom_selector] if custom_selector else self._AUTOCOMPLETE_OPTION_SELECTORS

        for sel in selectors_to_try:
            try:
                locator = self.page.locator(sel)
                count = await locator.count()
                if count > 0:
                    return [locator.nth(i) for i in range(min(count, 10))]  # cap at 10
            except Exception:
                continue

        return []

    async def _find_best_option(
        self,
        options: list[Locator],
        target: str,
    ) -> Locator | None:
        """Find the dropdown option whose text best matches the target value."""
        target_lower = " ".join(target.lower().split())
        best_locator: Locator | None = None
        best_score = 0.0

        for opt in options:
            try:
                text = await opt.inner_text(timeout=2000)
                text_lower = " ".join(text.lower().split())

                # Exact containment (target in option text or vice versa)
                if target_lower in text_lower or text_lower in target_lower:
                    # Prefer shorter options that still contain the target (more specific)
                    score = len(target_lower) / max(len(text_lower), 1)
                    if score > best_score:
                        best_score = score
                        best_locator = opt
            except Exception:
                continue

        # Require at least some overlap
        return best_locator if best_score > 0 else None

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

        # For single-digit TOTP values (from multi-field TOTP inputs), force fallback mode
        # so that we use the exact digit value instead of having AI generate a new one
        if value and len(value) == 1 and value.isdigit() and ai == "proactive":
            ai = "fallback"

        # format the text with the actual value of the parameter if it's a secret when running a workflow
        if ai == "fallback":
            error_to_raise = None
            original_value = value
            if selector:
                try:
                    value = await self.get_actual_value(
                        value,
                        totp_identifier=totp_identifier,
                        totp_url=totp_url,
                    )
                    locator = self.page.locator(selector).first
                    await self._prepare_element(locator, timeout=timeout)
                    # Use locator.fill() (programmatic, single-shot) instead of typing
                    # character-by-character.  Sequential typing triggers autocomplete
                    # dropdowns on search bars and typeaheads which destabilise the DOM
                    # and cause the locator to time out mid-input.
                    await locator.fill(value, timeout=timeout)
                    return original_value
                except Exception as e:
                    LOG.warning(
                        "CSS selector fill failed, falling back to AI",
                        selector=selector,
                        error=str(e),
                    )
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
                return original_value
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

        locator = self.page.locator(selector).first
        await locator.fill(value, timeout=timeout)
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
                    file_path = await download_file_from_url(files)
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

        file_path = await download_file_from_url(files)
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

    @action_wrap(ActionType.DOWNLOAD_FILE)
    async def download_file(
        self,
        file_name: str | None = None,
        download_url: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Download a file from a URL and save it locally during cached script replay.

        Args:
            file_name: The original file name (for logging/reference). Defaults to UUID if empty.
            download_url: The URL to download the file from.

        Returns:
            The local file path where the file was saved.
        """
        if not download_url:
            raise ValueError("download_url is required for download_file action in cached scripts")

        # Use uuid as fallback for empty file_name, matching handler.py behavior
        file_name = file_name or str(uuid.uuid4())

        file_path = await download_file_from_url(download_url, filename=file_name)
        return file_path

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

    async def validate(
        self,
        prompt: str,
        model: dict[str, Any] | str | None = None,
    ) -> bool:
        """Validate the current page state using AI.

        Args:
            prompt: Validation criteria or condition to check
            model: Optional model configuration. Can be either:
                   - A dict with model configuration (e.g., {"model_name": "gemini-2.5-flash-lite", "max_tokens": 2048})
                   - A string with just the model name (e.g., "gpt-4")

        Returns:
            bool: True if validation passes, False otherwise

        Examples:
            ```python
            # Simple validation
            is_valid = await page.validate("Check if the login was successful")

            # Validation with specific model (as string)
            is_valid = await page.validate(
                "Check if the order was placed",
                model="gemini-2.5-flash-lite"
            )

            # Validation with model config (as dict)
            is_valid = await page.validate(
                "Check if the payment completed",
                model={"model_name": "gemini-2.5-flash-lite", "max_tokens": 1024}
            )
            ```
        """
        normalized_model: dict[str, Any] | None = None
        if isinstance(model, str):
            normalized_model = {"model_name": model}
        elif model is not None:
            normalized_model = model

        return await self._ai.ai_validate(prompt=prompt, model=normalized_model)

    async def classify(
        self,
        options: dict[str, str],
        url_patterns: dict[str, str] | None = None,
        text_patterns: dict[str, str | list[str]] | None = None,
    ) -> str:
        """Classify the current page state against named options using a tiered cascade.

        This method uses a three-tier approach to minimize cost:
        1. URL pattern matching against current URL (FREE)
        2. Text substring check in page content (FREE)
        3. LLM-based classification as last resort (~$0.001)

        Args:
            options: Dict mapping option keys to descriptions.
                     e.g. {"eligible": "The page shows eligibility confirmation",
                            "not_eligible": "The page shows a rejection message"}
            url_patterns: Optional dict mapping option keys to regex patterns to match
                         against the current URL.
            text_patterns: Optional dict mapping option keys to text substrings to search
                          for in the page's extracted text.

        Returns:
            The matching option key, or "UNKNOWN" if no option matches.

        Examples:
            ```python
            state = await page.classify(
                options={
                    "success": "Form was submitted successfully",
                    "error": "Form submission failed with errors",
                    "captcha": "A CAPTCHA challenge appeared",
                },
                url_patterns={
                    "success": r"/confirmation",
                    "error": r"/error",
                },
                text_patterns={
                    "success": "Thank you for your submission",
                    "captcha": "Please verify you are human",
                },
            )

            if state == "success":
                # handle success path
                pass
            elif state == "error":
                # handle error path
                pass
            else:
                await page.element_fallback(navigation_goal="Complete the form submission")
            ```
        """
        return await self._ai.ai_classify(
            options=options,
            url_patterns=url_patterns,
            text_patterns=text_patterns,
        )

    async def scan_form_fields(self) -> list[dict[str, Any]]:
        """Scan the page for visible form fields using DOM inspection (no LLM).

        Two-pass approach:
        - Pass 1: Process non-checkbox/non-radio elements (text, email, select, textarea, etc.)
        - Pass 2: Group checkbox/radio inputs by their ``name`` attribute into
          ``checkbox_group`` / ``radio_group`` entries with an ``options`` list.

        Returns a list of field descriptors. Regular fields:
        [{"label": "Full name", "selector": "label:has-text('Full name') input",
          "tag": "input", "type": "text", "name": "full_name", "required": True}, ...]

        Group fields:
        [{"label": "Areas of focus", "selector": "input[name='field0']",
          "tag": "input", "type": "checkbox_group", "name": "field0", "required": False,
          "options": [{"label": "Engineering", "value": "Engineering", "selector": "..."}]}]
        """
        return await self.page.evaluate(
            """() => {
            const fields = [];
            const seen = new Set();

            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && style.opacity !== '0' && el.offsetWidth > 0 && el.offsetHeight > 0;
            }

            function getLabel(el) {
                if (el.id) {
                    const lbl = document.querySelector('label[for="' + el.id + '"]');
                    if (lbl) return lbl.textContent.trim();
                }
                const parentLabel = el.closest('label');
                if (parentLabel) return parentLabel.textContent.trim();
                if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
                const labelledBy = el.getAttribute('aria-labelledby');
                if (labelledBy) {
                    const ref = document.getElementById(labelledBy);
                    if (ref) return ref.textContent.trim();
                }
                if (el.placeholder) return el.placeholder;
                return null;
            }

            function buildSelector(el, label) {
                const tag = el.tagName.toLowerCase();
                // Prefer stable attribute selectors over fragile label text.
                // name/id selectors survive DOM re-renders and label text changes.
                if (el.name) return tag + '[name="' + el.name + '"]:visible';
                if (el.id) return '#' + el.id + ':visible';
                if (label && label.length < 80) {
                    const escapedLabel = label.replace(/'/g, "\\\\'");
                    const parentLabel = el.closest('label');
                    if (parentLabel || (el.id && document.querySelector('label[for="' + el.id + '"]'))) {
                        return 'label:has-text(\\'' + escapedLabel + '\\') ' + tag + ':visible';
                    }
                    if (el.getAttribute('aria-label')) {
                        return tag + '[aria-label="' + escapedLabel + '"]:visible';
                    }
                }
                return null;
            }

            function buildOptionSelector(el) {
                if (el.id) return '#' + el.id;
                const tag = el.tagName.toLowerCase();
                const name = el.name;
                const value = el.value;
                if (name && value) return tag + '[name="' + name + '"][value="' + value + '"]';
                if (name) return tag + '[name="' + name + '"]';
                return null;
            }

            // Find the group-level label for a set of checkbox/radio elements
            function getGroupLabel(elements) {
                if (!elements.length) return null;
                const first = elements[0];

                // 1. <fieldset><legend> wrapping the group
                const fieldset = first.closest('fieldset');
                if (fieldset) {
                    const legend = fieldset.querySelector('legend');
                    if (legend) return legend.textContent.trim();
                }

                // 2. Find nearest common ancestor, then look for a heading/label before it
                let ancestor = first.parentElement;
                const allInAncestor = () => elements.every(el => ancestor && ancestor.contains(el));
                while (ancestor && !allInAncestor()) {
                    ancestor = ancestor.parentElement;
                }
                if (ancestor) {
                    // aria-label on container
                    if (ancestor.getAttribute('aria-label')) return ancestor.getAttribute('aria-label');
                    // aria-labelledby on container
                    const lblBy = ancestor.getAttribute('aria-labelledby');
                    if (lblBy) {
                        const ref = document.getElementById(lblBy);
                        if (ref) return ref.textContent.trim();
                    }
                    // Look for heading or label element immediately before the container
                    let prev = ancestor.previousElementSibling;
                    if (prev) {
                        const tagName = prev.tagName.toLowerCase();
                        if (['label', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'span', 'div'].includes(tagName)) {
                            const text = prev.textContent.trim();
                            if (text && text.length < 200) return text;
                        }
                    }
                }

                // 3. Fall back to the first element's label
                return getLabel(first);
            }

            const elements = document.querySelectorAll('input, select, textarea');
            // Collect checkbox/radio inputs for pass 2
            const checkRadioGroups = {};

            for (const el of elements) {
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
                if (!isVisible(el)) continue;

                // Pass 2 collection: group checkboxes/radios by shared name attribute.
                // Checkboxes/radios without a name can't be reliably grouped —
                // emit them as individual fields (type: "checkbox" / "radio").
                if (type === 'checkbox' || type === 'radio') {
                    if (el.name) {
                        if (!checkRadioGroups[el.name]) {
                            checkRadioGroups[el.name] = { type: type, elements: [] };
                        }
                        checkRadioGroups[el.name].elements.push(el);
                    } else {
                        // Individual checkbox/radio without a name — emit directly
                        const label = getLabel(el);
                        const selector = buildSelector(el, label);
                        if (selector) {
                            fields.push({
                                label: label || null,
                                selector: selector,
                                tag: 'input',
                                type: type,
                                name: null,
                                required: el.required || false,
                                placeholder: null,
                            });
                        }
                    }
                    continue;
                }

                // Pass 1: non-checkbox/non-radio elements
                const uid = el.name || el.id || el.getAttribute('aria-label') || Math.random().toString();
                if (seen.has(uid)) continue;
                seen.add(uid);

                const label = getLabel(el);
                const selector = buildSelector(el, label);
                if (!selector) continue;

                fields.push({
                    label: label || null,
                    selector: selector,
                    tag: el.tagName.toLowerCase(),
                    type: type || (el.tagName.toLowerCase() === 'select' ? 'select' : el.tagName.toLowerCase() === 'textarea' ? 'textarea' : 'text'),
                    name: el.name || null,
                    required: el.required || false,
                    placeholder: el.placeholder || null,
                });

                // Collect <select> options for batch planning
                if (el.tagName.toLowerCase() === 'select') {
                    const selectOptions = [];
                    for (const opt of el.options) {
                        const optText = opt.textContent.trim();
                        if (!opt.value || opt.value === '' || optText === '' || optText === '--') continue;
                        selectOptions.push({
                            label: optText,
                            value: opt.value,
                        });
                    }
                    if (selectOptions.length > 0) {
                        fields[fields.length - 1].options = selectOptions;
                    }
                }
            }

            // Pass 2: emit grouped checkbox/radio entries
            for (const [groupKey, group] of Object.entries(checkRadioGroups)) {
                const els = group.elements;
                if (seen.has(groupKey)) continue;
                seen.add(groupKey);

                const groupLabel = getGroupLabel(els);
                const firstSelector = buildOptionSelector(els[0]) || buildSelector(els[0], getLabel(els[0]));
                if (!firstSelector) continue;

                const options = [];
                for (const el of els) {
                    const optLabel = getLabel(el) || el.value || null;
                    const optSelector = buildOptionSelector(el);
                    if (!optSelector) continue;
                    options.push({
                        label: optLabel,
                        value: el.value || null,
                        selector: optSelector,
                    });
                }

                const groupType = group.type === 'radio' ? 'radio_group' : 'checkbox_group';
                fields.push({
                    label: groupLabel || null,
                    selector: firstSelector,
                    tag: 'input',
                    type: groupType,
                    name: els[0].name || null,
                    required: els[0].required || false,
                    placeholder: null,
                    options: options,
                });
            }
            return fields;
        }"""
        )

    async def fill_form(
        self,
        field_map: dict[str, dict],
        context: Any,
        *,
        navigation_goal: str = "Fill out the form",
    ) -> None:
        """Scan page for form fields and fill them using the field_map.

        Two-pass structural anti-scrambling approach:
        - Pass 1: Resolve canonical extracted values and direct parameter values.
          These fields are removed from the batch planner's view entirely.
        - Pass 2: Batch plan ONLY unresolved fields (the planner can't scramble
          canonical values because it never sees them).
        - Pass 3: Fill everything.

        field_map keys are descriptive snake_case names (e.g., "full_name", "phone").
        field_map values are dicts with:
          - "param": parameter key in context.parameters (or None)
          - "action": "fill" | "select" | "fill_autocomplete" | "click" | "upload_file"
          - "ai": optional, "fallback" | "proactive" (default: "fallback" when param exists, "proactive" otherwise)
          - "prompt": AI prompt for this field
          - "labels": list of known label variants (used for fuzzy matching)

        Fields found on page but NOT in field_map are filled with ai='proactive'.
        """
        fields = await self.scan_form_fields()

        LOG.info(
            "fill_form: scanned page fields",
            field_count=len(fields),
            field_map_size=len(field_map),
        )

        # PASS 1: Structurally resolve canonical + direct-param fields
        resolved: dict[int, tuple[Any, dict]] = {}  # i -> (value, entry)
        unresolved_fields: list[tuple[int, dict, dict | None]] = []  # (i, field, matched_entry)

        for i, field in enumerate(fields):
            matched_entry = self._match_field_to_map(field, field_map, context)

            # Canonical extracted value → structural fill, skip batch planner
            if matched_entry and matched_entry.get("_extracted_value") is not None:
                LOG.info(
                    "fill_form: structurally resolved (canonical)",
                    field_label=field.get("label"),
                    category=matched_entry.get("_canonical"),
                    value_preview=str(matched_entry["_extracted_value"])[:40],
                )
                resolved[i] = (matched_entry["_extracted_value"], matched_entry)
                continue

            # Direct param value → structural fill, skip batch planner
            if matched_entry:
                param = matched_entry.get("param")
                direct_value = context.parameters.get(param) if param else None
                if direct_value:
                    LOG.info(
                        "fill_form: structurally resolved (param)",
                        field_label=field.get("label"),
                        param=param,
                    )
                    resolved[i] = (direct_value, matched_entry)
                    continue

            unresolved_fields.append((i, field, matched_entry))

        LOG.info(
            "fill_form: structural resolution complete",
            resolved_count=len(resolved),
            unresolved_count=len(unresolved_fields),
        )

        # PASS 2: Batch plan ONLY unresolved fields (no canonical fields in the prompt)
        planned_values: dict[int, Any] | None = None
        if unresolved_fields:
            only_fields = [f for _, f, _ in unresolved_fields]
            planned_values = await self._batch_plan_form_values(only_fields, field_map, context, navigation_goal)

        # PASS 3: Fill everything (with error tracking for element retry)
        # Pre-build lookup: original field index -> (unresolved_idx, matched_entry)
        original_to_unresolved: dict[int, tuple[int, dict | None]] = {
            fi: (ui, entry) for ui, (fi, _, entry) in enumerate(unresolved_fields)
        }

        failed_fields: list[tuple[int, dict]] = []  # (field_index, field) pairs that failed

        for i, field in enumerate(fields):
            try:
                if i in resolved:
                    value, entry = resolved[i]
                    await self._fill_with_planned_value(field, value, entry, navigation_goal=navigation_goal)
                elif i in original_to_unresolved:
                    unresolved_idx, matched = original_to_unresolved[i]
                    if planned_values and unresolved_idx in planned_values:
                        await self._fill_with_planned_value(
                            field, planned_values[unresolved_idx], matched, navigation_goal=navigation_goal
                        )
                    elif matched:
                        # Try to resolve a value from param/extracted before falling back to per-field AI
                        fallback_value = self._resolve_fallback_value(field, matched, context)
                        if fallback_value is not None:
                            await self._fill_with_planned_value(
                                field, fallback_value, matched, navigation_goal=navigation_goal
                            )
                        else:
                            await self._fill_matched_field(field, matched, context, navigation_goal)
                    else:
                        await self._fill_unknown_field(field, navigation_goal)
                else:
                    await self._fill_unknown_field(field, navigation_goal)
            except Exception:
                LOG.warning(
                    "fill_form: field failed during pass 3, will retry",
                    field_label=field.get("label"),
                    field_index=i,
                    exc_info=True,
                )
                failed_fields.append((i, field))

        # PASS 4: Element retry of failed fields only (prevents full-block AI fallback)
        if failed_fields:
            LOG.info(
                "fill_form: retrying failed fields with AI",
                failed_count=len(failed_fields),
            )
            for i, field in failed_fields:
                try:
                    await self._fill_unknown_field(field, navigation_goal)
                except Exception:
                    LOG.warning(
                        "fill_form: element retry also failed",
                        field_label=field.get("label"),
                        exc_info=True,
                    )

        # QUALITY AUDIT: LLM-based verification (test-only, gated by env var)
        if os.environ.get("SCRIPT_QUALITY_AUDIT"):
            await self.quality_audit(context, navigation_goal)

    def _match_field_to_map(
        self,
        field: dict[str, Any],
        field_map: dict[str, dict],
        context: Any = None,
    ) -> dict | None:
        """Fuzzy-match a scanned field against field_map entries, then canonical categories.

        Matching strategy (in order):
        1. Exact/substring match against FIELD_MAP labels (existing)
        2. Canonical category match (zero LLM cost)
        3. Word overlap >= 50% against FIELD_MAP labels (existing)
        """
        field_label = (field.get("label") or field.get("name") or field.get("placeholder") or "").lower().strip()
        if not field_label:
            return None

        field_words = set(field_label.split())

        best_match: dict | None = None
        best_score = 0.0
        best_word_match: dict | None = None
        best_word_score = 0.0

        for entry in field_map.values():
            labels = entry.get("labels", [])
            for known_label in labels:
                known_lower = known_label.lower().strip()
                known_words = set(known_lower.split())

                # Priority 1: Exact match
                if field_label == known_lower:
                    return entry

                # Priority 2: Substring containment
                if known_lower in field_label or field_label in known_lower:
                    score = len(known_lower) / max(len(field_label), 1)
                    if score > best_score:
                        best_score = score
                        best_match = entry
                    continue

                # Priority 4 (collected, checked after canonical): Word overlap
                if field_words and known_words:
                    overlap = len(field_words & known_words)
                    total = max(len(field_words), len(known_words))
                    score = overlap / total
                    if score >= 0.5 and score > best_word_score:
                        best_word_score = score
                        best_word_match = entry

        # Return substring match if found (priority 2)
        if best_match:
            return best_match

        # Priority 3: Canonical category match
        category = match_field_to_category(field_label)
        if category:
            # Build a synthetic entry from the canonical category
            extracted_value = None
            if context and hasattr(context, "extracted_params"):
                extracted_value = context.extracted_params.get(category.name)

            # Prefer direct parameter value over LLM-extracted value
            if category.param and context and hasattr(context, "parameters"):
                param_value = context.parameters.get(category.param)
                if param_value is not None:
                    extracted_value = None  # let _fill_matched_field use the param directly

            entry = {
                "param": category.param,
                "action": category.action,
                "labels": list(category.keywords),
                "prompt": category.prompt,
                "ai": "fallback" if (category.param or extracted_value is not None) else "proactive",
                "_canonical": category.name,
                "_extracted_value": extracted_value,
            }
            LOG.info(
                "fill_form: canonical category match",
                field_label=field_label,
                category=category.name,
                has_extracted_value=extracted_value is not None,
            )
            return entry

        # Priority 4: Word overlap
        return best_word_match

    @staticmethod
    def _resolve_fallback_value(
        field: dict[str, Any],
        entry: dict,
        context: Any,
    ) -> str | list | bool | None:
        """Try to resolve a fill value from param/extracted data without using AI.

        For click_groups (radio/checkbox) and selects, tries text-matching the
        param value or extracted value against available options. This avoids
        falling through to _fill_matched_field which makes N individual AI calls.

        Returns the resolved value or None if no structural match is possible.
        """
        field_type = field.get("type", "text")

        # Only attempt for fields with options (radio_group, checkbox_group, select)
        options = field.get("options")
        has_select_options = field.get("tag", "").lower() == "select" and options
        has_group_options = field_type in ("radio_group", "checkbox_group") and options

        if not has_select_options and not has_group_options:
            return None

        # Get candidate value from param or extracted_value
        candidate = None
        param = entry.get("param")
        if param and context and hasattr(context, "parameters"):
            candidate = context.parameters.get(param)
        if candidate is None:
            candidate = entry.get("_extracted_value")
        if candidate is None:
            return None

        candidate_str = str(candidate).lower().strip()
        if not candidate_str:
            return None

        # Try to text-match against options
        opts: list[dict] = options if isinstance(options, list) else []
        option_labels = [(o.get("label") or o.get("value", "")).lower().strip() for o in opts]

        def _return_option(idx: int) -> str | list:
            original_label = opts[idx].get("label") or opts[idx].get("value", "")
            if field_type == "checkbox_group":
                return [original_label]
            return original_label

        # Exact match
        for i, opt_label in enumerate(option_labels):
            if candidate_str == opt_label:
                return _return_option(i)

        # Value mappings match (canonical EEO fields)
        # Check both directions: candidate matches mapping_key, OR candidate
        # matches any of the mapping_labels (handles "prefer not to answer" -> decline).
        category_name = entry.get("_canonical")
        if category_name:
            cat_obj = get_category(category_name)
            if cat_obj and cat_obj.value_mappings:
                for mapping_key, mapping_labels in cat_obj.value_mappings:
                    key_matches = mapping_key in candidate_str or candidate_str in mapping_key
                    label_matches = any(ml in candidate_str or candidate_str in ml for ml in mapping_labels)
                    if key_matches or label_matches:
                        # Find option matching any of the mapping labels
                        for i, opt_label in enumerate(option_labels):
                            if any(ml in opt_label for ml in mapping_labels):
                                LOG.info(
                                    "resolve_fallback: value_mappings match",
                                    candidate=candidate_str,
                                    mapping_key=mapping_key,
                                    matched_option=opt_label,
                                )
                                return _return_option(i)

        # Polarity-aware match for binary EEO categories.
        # Picks the option with the highest word overlap with the candidate among
        # options that share the same polarity, avoiding wrong first-match.
        _POLARITY_CATEGORIES = {"veteran_status", "disability", "work_authorization"}
        if category_name in _POLARITY_CATEGORIES:
            _NEGATIVE_SIGNALS = {"no", "not", "don't", "doesn't", "none", "neither", "decline", "prefer not"}
            candidate_is_negative = any(sig in candidate_str for sig in _NEGATIVE_SIGNALS)
            candidate_words = set(candidate_str.split())
            best_idx = -1
            best_overlap = -1
            for i, opt_label in enumerate(option_labels):
                opt_is_negative = any(sig in opt_label for sig in _NEGATIVE_SIGNALS)
                if candidate_is_negative == opt_is_negative:
                    opt_words = set(opt_label.split())
                    overlap = len(candidate_words & opt_words)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_idx = i
            if best_idx >= 0:
                LOG.info(
                    "resolve_fallback: polarity match",
                    candidate=candidate_str,
                    matched_option=option_labels[best_idx],
                    polarity="negative" if candidate_is_negative else "positive",
                    word_overlap=best_overlap,
                )
                return _return_option(best_idx)

        # Substring containment match (general fallback)
        for i, opt_label in enumerate(option_labels):
            if candidate_str in opt_label or opt_label in candidate_str:
                return _return_option(i)

        return None

    @staticmethod
    def _build_alt_selector(field: dict[str, Any]) -> str | None:
        """Build an alternate selector from field metadata when the primary selector fails.

        Tries name attribute, then id, to produce a selector different from the primary.
        """
        tag = field.get("tag", "input").lower()
        name = field.get("name")
        if name:
            # Only allow safe CSS attribute value characters
            if not re.fullmatch(r"[a-zA-Z0-9_\-.\[\]]+", name):
                return None
            return f'{tag}[name="{name}"]:visible'
        return None

    @staticmethod
    def _resolve_method(
        intent: str,
        field_type: str,
        field_tag: str,
    ) -> str:
        """Resolve the actual method to call based on field intent + real HTML type.

        The FIELD_MAP ``action`` is a semantic intent (what should happen).
        The actual method depends on the real DOM element type.

        Override rules (field type takes precedence):
        - checkbox/checkbox_group → always "click" / "click_group"
        - radio/radio_group → always "click" / "click_group"
        - <select> tag → always "select_option"
        - fill_autocomplete intent → always honored (special interaction)
        - Otherwise → use the FIELD_MAP intent as-is
        """
        ft = field_type.lower()
        tag = field_tag.lower()

        # Groups always use click_group
        if ft in ("checkbox_group", "radio_group"):
            return "click_group"

        # Individual checkbox/radio always use click
        if ft in ("checkbox", "radio"):
            return "click"

        # File inputs always use upload_file
        if ft == "file":
            return "upload_file"

        # <select> tag always uses select_option
        if tag == "select":
            return "select_option"

        # fill_autocomplete is a special interaction pattern — always honored
        if intent == "fill_autocomplete":
            return "fill_autocomplete"

        # Map "select" intent to "select_option" for non-select elements
        # (e.g., a text field with intent "select" should just fill)
        if intent == "select" and tag != "select":
            return "fill"

        return intent

    async def _ai_select_from_group(
        self,
        field: dict[str, Any],
        navigation_goal: str,
        entry: dict | None = None,
    ) -> list[int]:
        """Single LLM call to select option(s) from a radio/checkbox group.

        Returns a list of 0-indexed option indices to click.
        Falls back to empty list on failure (caller should handle).
        """
        from skyvern.core.script_generations.real_skyvern_page_ai import _get_context_data

        options = field.get("options", [])
        if not options:
            return []

        label = field.get("label") or field.get("name") or field.get("placeholder") or "unknown field"
        field_type = field.get("type", "radio_group")
        option_labels = [o.get("label") or o.get("value", "") for o in options]
        data = _get_context_data(None)

        prompt = prompt_engine.load_prompt(
            template="select-from-group",
            label=label,
            field_type=field_type,
            options=option_labels,
            data=data,
            goal=navigation_goal,
        )

        try:
            skyvern_ctx = skyvern_context.current()
            org_id = skyvern_ctx.organization_id if skyvern_ctx else None
            if skyvern_ctx:
                skyvern_ctx.script_llm_call_count += 1
            json_response = await app.SECONDARY_LLM_API_HANDLER(
                prompt=prompt,
                prompt_name="select-from-group",
                organization_id=org_id,
            )
            selected = json_response.get("selected", [])
            # Convert 1-indexed to 0-indexed, validate bounds
            indices = [int(s) - 1 for s in selected if 0 < int(s) <= len(options)]
            LOG.info(
                "ai_select_from_group: resolved",
                label=label,
                selected_indices=indices,
                selected_labels=[option_labels[i] for i in indices],
            )
            return indices
        except Exception:
            LOG.warning("ai_select_from_group failed", label=label, exc_info=True)
            return []

    async def _fill_group_with_ai(
        self,
        field: dict[str, Any],
        navigation_goal: str,
        entry: dict | None = None,
    ) -> None:
        """Fill a radio/checkbox group using a single AI call instead of N per-option calls."""
        selected_indices = await self._ai_select_from_group(field, navigation_goal, entry)
        if selected_indices:
            for idx in selected_indices:
                await self.click(selector=field["options"][idx]["selector"], ai=None)
        else:
            # Single-call failed — log warning but do NOT fall back to per-option AI calls.
            # This prevents N individual LLM calls that defeat the cost-saving purpose.
            # The field will be caught by the retry pass or agent fallback.
            label = field.get("label") or field.get("name") or "unknown"
            LOG.warning(
                "fill_group_with_ai: single-call returned empty, skipping group",
                label=label,
                option_count=len(field.get("options", [])),
            )

    async def _fill_matched_field(
        self,
        field: dict[str, Any],
        entry: dict,
        context: Any,
        navigation_goal: str = "Fill out the form",
    ) -> None:
        """Fill a single form field using a matched field_map entry.

        Uses ``_resolve_method()`` to determine the correct browser method based
        on the runtime HTML element type, not just the FIELD_MAP ``action`` intent.
        """
        intent = entry.get("action", "fill")
        param = entry.get("param")
        ai = entry.get("ai", "fallback" if param else "proactive")
        prompt = entry.get("prompt", "")
        selector = field.get("selector", "")

        method = self._resolve_method(
            intent=intent,
            field_type=field.get("type", "text"),
            field_tag=field.get("tag", "input"),
        )

        value = context.parameters.get(param) if param else None

        try:
            if method == "click_group":
                # Single AI call for the whole group instead of N per-option calls
                await self._fill_group_with_ai(field, navigation_goal, entry)
            elif method == "click":
                if ai in ("proactive", "fallback"):
                    self._track_ai_call()
                await self.click(selector=selector, ai=ai, prompt=prompt)
            elif method == "select_option":
                if ai == "proactive":
                    self._track_ai_call()
                    await self.select_option(selector=selector, ai="proactive", prompt=prompt)
                else:
                    self._track_ai_call()
                    await self.select_option(selector=selector, value=value, ai="fallback", prompt=prompt)
            elif method == "upload_file":
                if ai in ("proactive", "fallback"):
                    self._track_ai_call()
                file_value = value or ""
                await self.upload_file(selector=selector, files=file_value, ai=ai, prompt=prompt)
            elif method == "fill_autocomplete":
                if ai == "proactive":
                    self._track_ai_call()
                    await self.fill_autocomplete(selector=selector, ai="proactive", prompt=prompt)
                else:
                    self._track_ai_call()
                    await self.fill_autocomplete(selector=selector, value=value, ai="fallback", prompt=prompt)
            else:
                # Default: fill
                if ai == "proactive":
                    self._track_ai_call()
                    await self.fill(selector=selector, ai="proactive", prompt=prompt)
                else:
                    self._track_ai_call()
                    await self.fill(selector=selector, value=value, ai="fallback", prompt=prompt)
        except Exception:
            LOG.warning(
                "fill_form: failed to fill matched field, trying AI fallback",
                selector=selector,
                method=method,
                intent=intent,
                param=param,
                exc_info=True,
            )
            if prompt:
                self._track_ai_call()
                await self.fill(ai="proactive", prompt=prompt)

    async def _fill_unknown_field(
        self,
        field: dict[str, Any],
        navigation_goal: str,
    ) -> None:
        """Fill an unknown field (not in field_map) using AI proactive mode.

        Uses ``_resolve_method()`` to pick the correct browser method based on
        the actual HTML element type (checkbox_group, radio, select, etc.).
        """
        label = field.get("label") or field.get("name") or field.get("placeholder") or "unknown field"
        selector = field.get("selector", "")
        field_type = field.get("type", "text")
        field_tag = field.get("tag", "input")

        prompt = f"Fill the '{label}' field as part of: {navigation_goal}"

        method = self._resolve_method(intent="fill", field_type=field_type, field_tag=field_tag)

        try:
            if method == "click_group":
                # Single AI call for the whole group instead of N per-option calls
                await self._fill_group_with_ai(field, navigation_goal)
            elif method == "click":
                self._track_ai_call()
                await self.click(selector=selector, ai="proactive", prompt=prompt)
            elif method == "upload_file":
                self._track_ai_call()
                await self.upload_file(selector=selector, ai="proactive", prompt=prompt)
            elif method == "select_option":
                self._track_ai_call()
                await self.select_option(selector=selector, ai="proactive", prompt=prompt)
            else:
                self._track_ai_call()
                await self.fill(selector=selector, ai="proactive", prompt=prompt)
        except Exception:
            LOG.warning(
                "fill_form: failed to fill unknown field",
                label=label,
                selector=selector,
                method=method,
                exc_info=True,
            )

    async def _batch_plan_form_values(
        self,
        fields: list[dict[str, Any]],
        field_map: dict[str, dict],
        context: Any,
        navigation_goal: str,
    ) -> dict[int, str] | None:
        """Plan values for unresolved form fields in a single LLM call.

        Only called with fields that were NOT structurally resolved (canonical
        extracted values and direct parameter values are excluded upstream).
        The planner never sees resolved fields, so it can't scramble their values.

        Returns a mapping of field index (0-based within this list) -> planned value,
        or None on failure.
        """
        from skyvern.core.script_generations.real_skyvern_page_ai import _get_context_data

        if not fields:
            return None

        data = _get_context_data(None)

        # Build field descriptions for the prompt
        FORMAT_HINTS = {
            ("input", "text"): "short text",
            ("input", "email"): "email address",
            ("input", "url"): "URL",
            ("input", "tel"): "phone number",
            ("input", "date"): "date",
            ("input", "number"): "number",
            ("input", "file"): "file upload",
            ("textarea", ""): "paragraph/essay",
            ("textarea", "textarea"): "paragraph/essay",
            ("select", ""): "dropdown select",
            ("select", "select"): "dropdown select",
        }

        field_descs = []
        for field in fields:
            label = field.get("label") or field.get("name") or field.get("placeholder") or "unknown"
            field_type = field.get("type", "text")
            field_tag = field.get("tag", "input")

            # Build format hint
            if field_type in ("checkbox_group",):
                hint = "multi-select checkboxes"
            elif field_type in ("radio_group",):
                hint = "single choice radio"
            elif field_type in ("checkbox",):
                hint = "single checkbox (true/false)"
            else:
                hint = FORMAT_HINTS.get((field_tag, field_type), FORMAT_HINTS.get((field_tag, ""), field_type))

            options = None
            if field.get("options"):
                options = [o.get("label") or o.get("value", "") for o in field["options"]]

            field_descs.append(
                {
                    "label": label,
                    "format_hint": hint,
                    "options": options,
                    "required": field.get("required", False),
                    "placeholder": field.get("placeholder"),
                }
            )

        # Attach format hints to original field dicts for downstream use
        for i, field in enumerate(fields):
            if i < len(field_descs):
                field["_format_hint"] = field_descs[i].get("format_hint", "text")

        # Resolve Jinja-style {{ key }} templates in navigation_goal with actual data
        resolved_goal = navigation_goal
        if data and isinstance(data, dict):
            for key, value in data.items():
                if value is not None:
                    resolved_goal = resolved_goal.replace("{{ " + key + " }}", str(value))
                    resolved_goal = resolved_goal.replace("{{" + key + "}}", str(value))

        prompt = prompt_engine.load_prompt(
            template="batch-form-fill-plan",
            goal=resolved_goal,
            data=data,
            fields=field_descs,
        )

        try:
            skyvern_ctx = skyvern_context.current()
            org_id = skyvern_ctx.organization_id if skyvern_ctx else None
            if skyvern_ctx:
                skyvern_ctx.script_llm_call_count += 1
            json_response = await app.SECONDARY_LLM_API_HANDLER(
                prompt=prompt,
                prompt_name="batch-form-fill-plan",
                organization_id=org_id,
            )
            # Convert string keys to int (1-indexed from prompt -> 0-indexed)
            # Filter out null values
            result = {}
            for k, v in json_response.items():
                if v is None:
                    continue
                try:
                    result[int(k) - 1] = v
                except (ValueError, TypeError):
                    LOG.warning("Non-numeric key in batch plan response, skipping", key=k)
                    continue
            return result
        except Exception:
            LOG.warning("batch_plan_form_values failed, falling back to per-field AI", exc_info=True)
            return None

    async def _fill_with_planned_value(
        self,
        field: dict[str, Any],
        planned_value: str | list | bool | None,
        entry: dict | None,
        navigation_goal: str = "Fill out the form",
    ) -> None:
        """Fill a field with a pre-planned value (no per-field LLM call)."""
        import json as _json

        selector = field.get("selector", "")
        field_type = field.get("type", "text")
        field_tag = field.get("tag", "input")
        intent = entry.get("action", "fill") if entry else "fill"
        label = field.get("label") or field.get("name") or field.get("placeholder") or "unknown field"
        hint = field.get("_format_hint", "text")

        method = self._resolve_method(intent=intent, field_type=field_type, field_tag=field_tag)

        try:
            if method == "click_group":
                # Normalize planned_value to a list of selected option labels
                if isinstance(planned_value, list):
                    selected_labels = [str(v).lower().strip() for v in planned_value]
                elif isinstance(planned_value, str):
                    # Try parsing as JSON array (batch planner may return '["A", "B"]')
                    try:
                        parsed = _json.loads(planned_value)
                        if isinstance(parsed, list):
                            selected_labels = [str(v).lower().strip() for v in parsed]
                        else:
                            selected_labels = [str(planned_value).lower().strip()]
                    except (ValueError, TypeError):
                        selected_labels = [str(planned_value).lower().strip()]
                elif isinstance(planned_value, bool):
                    # Single checkbox: true = check, false = skip
                    if planned_value and field.get("options"):
                        if len(field.get("options", [])) == 1:
                            await self.click(selector=field["options"][0]["selector"], ai=None)
                    return
                else:
                    selected_labels = [str(planned_value).lower().strip()]

                # Click options matching the selected list (fuzzy matching).
                # For each selected label, find the best matching option.
                matched_opt_indices: set[int] = set()
                options = field.get("options", [])
                opt_labels = [(o.get("label") or o.get("value", "")).lower().strip() for o in options]

                for sel in selected_labels:
                    # Pass 1: exact match
                    for oi, ol in enumerate(opt_labels):
                        if ol == sel:
                            matched_opt_indices.add(oi)
                            break
                    else:
                        # Pass 2: substring containment (either direction)
                        for oi, ol in enumerate(opt_labels):
                            if sel in ol or ol in sel:
                                matched_opt_indices.add(oi)
                                break
                        else:
                            # Pass 3: word overlap >= 50%
                            sel_words = set(sel.split())
                            best_oi, best_score = -1, 0.0
                            for oi, ol in enumerate(opt_labels):
                                ol_words = set(ol.split())
                                if not sel_words or not ol_words:
                                    continue
                                overlap = len(sel_words & ol_words)
                                score = overlap / max(len(sel_words), len(ol_words))
                                if score > best_score:
                                    best_score = score
                                    best_oi = oi
                            if best_oi >= 0 and best_score >= 0.5:
                                matched_opt_indices.add(best_oi)

                if matched_opt_indices:
                    for oi in sorted(matched_opt_indices):
                        await self.click(selector=options[oi]["selector"], ai=None)
                else:
                    LOG.warning(
                        "fill_with_planned_value: no option matched for click_group",
                        planned_labels=selected_labels,
                        available_labels=opt_labels,
                    )
                    # Fall through to the exception handler below for AI fallback
                    raise ValueError(f"No option matched for planned labels: {selected_labels}")
            elif method == "click":
                await self.click(selector=selector, ai=None)
            elif method == "select_option":
                # Planned values are typically option labels (display text) from the batch planner.
                # Try matching by label first, then fall back to value attribute.
                locator = self.page.locator(selector)
                try:
                    await locator.select_option(label=str(planned_value), timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
                except Exception:
                    await locator.select_option(str(planned_value), timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
            elif method == "upload_file":
                await self.upload_file(
                    selector=selector, files=str(planned_value), ai="fallback", prompt=f"Upload file for {label}"
                )
            elif method == "fill_autocomplete":
                await self.fill_autocomplete(selector=selector, value=str(planned_value), ai=None)
            else:
                await self.fill(selector=selector, value=str(planned_value), ai=None)
        except Exception as primary_err:
            # Try alternate selector before falling back to AI (zero LLM cost)
            alt_selector = self._build_alt_selector(field)
            if alt_selector and alt_selector != selector:
                try:
                    LOG.info(
                        "fill_with_planned_value: trying alternate selector",
                        original=selector,
                        alternate=alt_selector,
                        method=method,
                    )
                    if method == "fill_autocomplete":
                        await self.fill_autocomplete(selector=alt_selector, value=str(planned_value), ai=None)
                    elif method == "select_option":
                        locator = self.page.locator(alt_selector)
                        try:
                            await locator.select_option(
                                label=str(planned_value), timeout=settings.BROWSER_ACTION_TIMEOUT_MS
                            )
                        except Exception:
                            await locator.select_option(str(planned_value), timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
                    elif method == "fill":
                        await self.fill(selector=alt_selector, value=str(planned_value), ai=None)
                    else:
                        raise primary_err  # no alternate strategy for click_group etc.
                    return  # alternate selector worked — skip AI
                except Exception:
                    LOG.info("fill_with_planned_value: alternate selector also failed", alternate=alt_selector)

            LOG.warning(
                "fill_with_planned_value failed, falling back to AI",
                selector=selector,
                method=method,
                planned_value=planned_value,
                exc_info=True,
            )
            # Include the planned value in the prompt so AI can use it as a hint
            prompt = f"Fill the '{label}' field. Suggested value: '{planned_value}'. Field type: {field_type}."
            try:
                if method == "click_group":
                    # Single AI call for the whole group instead of N per-option calls
                    await self._fill_group_with_ai(field, navigation_goal, entry)
                elif method == "select_option":
                    self._track_ai_call()
                    await self.select_option(selector=selector, ai="proactive", prompt=prompt)
                else:
                    self._track_ai_call()
                    await self.fill(selector=selector, ai="proactive", prompt=prompt)
            except Exception:
                LOG.warning("fill_with_planned_value AI fallback also failed", label=label, exc_info=True)
            return

        # Post-fill validation for text fields: detect essays in short-text fields
        if method in ("fill", "fill_autocomplete") and hint == "short text":
            try:
                actual = await self.page.locator(selector).input_value(timeout=2000)
                if actual and len(actual) > 100:
                    LOG.warning(
                        "fill_with_planned_value: value too long for short text field, re-filling with AI",
                        label=label,
                        value_length=len(actual),
                    )
                    await self.fill(selector=selector, value="", ai=None)  # clear
                    self._track_ai_call()
                    await self.fill(
                        selector=selector,
                        ai="proactive",
                        prompt=f"Fill the '{label}' field with a SHORT value (not an essay). Field type: {hint}",
                    )
            except Exception:
                pass  # validation is best-effort, don't block on failure

    async def structural_validate(self) -> bool:
        """Validate form completion structurally — zero LLM cost.

        Re-scans form fields and checks:
        1. All required fields have non-empty values
        2. No visible error/invalid messages on the page

        Returns True if all checks pass (no LLM call needed).
        Returns False to signal the caller should fall back to LLM validation.
        """
        try:
            field_values = await self.page.evaluate(
                """() => {
                const results = [];
                const elements = document.querySelectorAll('input, select, textarea');

                function isVisible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && style.opacity !== '0' && el.offsetWidth > 0 && el.offsetHeight > 0;
                }

                for (const el of elements) {
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    if (['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(type)) continue;
                    if (!isVisible(el)) continue;

                    const required = el.required || el.getAttribute('aria-required') === 'true';
                    if (!required) continue;

                    let value = '';
                    const tag = el.tagName.toLowerCase();
                    if (tag === 'select') {
                        const selected = el.options[el.selectedIndex];
                        value = selected ? selected.value : '';
                    } else if (type === 'checkbox' || type === 'radio') {
                        // For radio/checkbox groups, check if any in the group is checked
                        if (el.name) {
                            const group = document.querySelectorAll(
                                'input[name="' + el.name + '"]:checked'
                            );
                            value = group.length > 0 ? 'checked' : '';
                        } else {
                            value = el.checked ? 'checked' : '';
                        }
                    } else {
                        value = el.value || '';
                    }

                    const label = el.name || el.id || el.getAttribute('aria-label') || tag;
                    results.push({required: true, hasValue: value.trim().length > 0, label: label});
                }
                return results;
            }"""
            )

            # Check all required fields have values
            for fv in field_values:
                if fv.get("required") and not fv.get("hasValue"):
                    LOG.info(
                        "structural_validate: required field is empty, falling back to LLM",
                        field=fv.get("label", "unknown"),
                    )
                    return False

            # Check for visible error messages
            error_count = await self.page.locator(
                "[class*='error']:visible, [class*='invalid']:visible, "
                "[role='alert']:visible, [aria-invalid='true']:visible"
            ).count()

            if error_count > 0:
                LOG.info(
                    "structural_validate: visible error elements found",
                    error_count=error_count,
                )
                return False

            LOG.info("structural_validate: all checks passed, skipping LLM validation")
            return True

        except Exception:
            LOG.warning("structural_validate: check failed, falling back to LLM", exc_info=True)
            return False

    async def quality_audit(self, context: Any, navigation_goal: str = "") -> dict[str, Any] | None:
        """Run an LLM-based quality audit of the filled form.

        Reads current field values from the DOM and sends them with the applicant
        data to an LLM. Returns a quality assessment dict or None on failure.

        Only runs when SCRIPT_QUALITY_AUDIT=1 env var is set (test-only).
        The cost of this call is logged with prompt_name="quality-audit" so the
        test harness can exclude it from cost metrics.
        """
        try:
            # Read current values of all visible form fields
            field_values = await self.page.evaluate(
                """() => {
                const results = [];
                const elements = document.querySelectorAll('input, select, textarea');
                const seen = new Set();

                function isVisible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && style.opacity !== '0' && el.offsetWidth > 0 && el.offsetHeight > 0;
                }

                function getLabel(el) {
                    if (el.id) {
                        const lbl = document.querySelector('label[for="' + el.id + '"]');
                        if (lbl) return lbl.textContent.trim();
                    }
                    const parentLabel = el.closest('label');
                    if (parentLabel) {
                        // Exclude text from child inputs
                        const clone = parentLabel.cloneNode(true);
                        clone.querySelectorAll('input,select,textarea').forEach(c => c.remove());
                        return clone.textContent.trim();
                    }
                    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
                    if (el.placeholder) return el.placeholder;
                    return el.name || el.id || null;
                }

                function getFieldValue(el) {
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const tag = el.tagName.toLowerCase();

                    if (tag === 'select') {
                        const selected = el.options[el.selectedIndex];
                        return selected ? selected.text.trim() : '';
                    }

                    if (type === 'checkbox') {
                        return el.checked ? 'checked' : 'unchecked';
                    }

                    if (type === 'radio') {
                        // Find the checked radio in the same group
                        if (el.name) {
                            const checked = document.querySelector(
                                'input[name="' + el.name + '"]:checked'
                            );
                            if (checked) {
                                const lbl = getLabel(checked);
                                return lbl || checked.value || 'selected';
                            }
                            return '(none selected)';
                        }
                        return el.checked ? 'selected' : 'not selected';
                    }

                    if (type === 'file') {
                        return el.files && el.files.length > 0
                            ? el.files[0].name : '(no file)';
                    }

                    return el.value || '';
                }

                for (const el of elements) {
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
                    if (!isVisible(el)) continue;

                    // Deduplicate radio groups by name
                    if (type === 'radio' && el.name) {
                        if (seen.has('radio:' + el.name)) continue;
                        seen.add('radio:' + el.name);
                    }

                    const label = getLabel(el);
                    const value = getFieldValue(el);
                    const tag = el.tagName.toLowerCase();

                    results.push({
                        label: label || '(unlabeled)',
                        type: type || tag,
                        value: value,
                    });
                }
                return results;
            }"""
            )

            if not field_values:
                LOG.info("quality_audit: no fields found on page")
                return None

            # Build applicant data string from context
            data_parts: list[str] = []
            if hasattr(context, "parameters"):
                for key, value in context.parameters.items():
                    if isinstance(value, str) and value:
                        data_parts.append(f"- {key}: {value}")
            applicant_data = "\n".join(data_parts) if data_parts else "(no data)"

            prompt_text = prompt_engine.load_prompt(
                template="quality-audit",
                data=applicant_data,
                fields=field_values,
            )

            skyvern_ctx = skyvern_context.current()
            org_id = skyvern_ctx.organization_id if skyvern_ctx else None

            result = await app.SECONDARY_LLM_API_HANDLER(
                prompt=prompt_text,
                prompt_name="quality-audit",
                organization_id=org_id,
            )

            if isinstance(result, dict):
                score = result.get("score", 0)
                issues = result.get("issues", [])
                summary = result.get("summary", "")

                LOG.info(
                    "quality_audit_result",
                    score=score,
                    field_count=result.get("field_count", len(field_values)),
                    correct_count=result.get("correct_count", 0),
                    issue_count=len(issues),
                    summary=summary,
                )

                # Log individual issues
                for issue in issues:
                    LOG.info(
                        "quality_audit_issue",
                        field_label=issue.get("field_label", ""),
                        severity=issue.get("severity", ""),
                        problem=issue.get("problem", ""),
                        expected=issue.get("expected", ""),
                        actual=issue.get("actual", ""),
                    )

                return result
            else:
                LOG.warning("quality_audit: unexpected LLM response type", result_type=type(result).__name__)
                return None

        except Exception:
            LOG.warning("quality_audit: audit failed", exc_info=True)
            return None

    async def element_fallback(
        self,
        navigation_goal: str,
        max_steps: int = 10,
    ) -> None:
        """Activate the AI agent from the CURRENT page position to achieve a goal.

        Instead of re-running the entire block when cached code encounters an unknown
        state, this method activates the AI agent from the current page position.
        Much cheaper than a full block re-execution.

        Args:
            navigation_goal: The goal for the AI agent to achieve from the current page.
            max_steps: Maximum number of agent steps before giving up. Defaults to 10.

        Raises:
            Exception: If the element fallback fails or exceeds max_steps.

        Examples:
            ```python
            state = await page.classify(options={...})
            if state == "known_path":
                # handle known path
                pass
            else:
                # Let the AI agent handle the unknown state
                await page.element_fallback(
                    navigation_goal="Complete the registration form"
                )
            ```
        """
        return await self._ai.ai_element_fallback(
            navigation_goal=navigation_goal,
            max_steps=max_steps,
        )

    async def prompt(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        model: dict[str, Any] | str | None = None,
    ) -> dict[str, Any] | list | str | None:
        """Send a prompt to the LLM and get a response based on the provided schema.

        This method allows you to interact with the LLM directly without requiring page context.
        It's useful for making decisions, generating text, or processing information using AI.

        Args:
            prompt: The prompt to send to the LLM
            schema: Optional JSON schema to structure the response. If provided, the LLM response
                   will be validated against this schema.
            model: Optional model configuration. Can be either:
                   - A dict with model configuration (e.g., {"model_name": "gemini-2.5-flash-lite", "max_tokens": 2048})
                   - A string with just the model name (e.g., "gemini-2.5-flash-lite")

        Returns:
            LLM response structured according to the schema if provided, or unstructured response otherwise.

        Examples:
            ```python
            # Simple unstructured prompt
            response = await page.prompt("What is 2 + 2?")
            # Returns: {'llm_response': '2 + 2 equals 4.'}

            # Structured prompt with schema
            response = await page.prompt(
                "What is 2 + 2?",
                schema={
                    "type": "object",
                    "properties": {
                        "result_number": {"type": "int"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                    }
                }
            )
            # Returns: {'result_number': 4, 'confidence': 1}
            ```
        """
        normalized_model: dict[str, Any] | None = None
        if isinstance(model, str):
            normalized_model = {"model_name": model}
        elif model is not None:
            normalized_model = model

        return await self._ai.ai_prompt(prompt=prompt, schema=schema, model=normalized_model)

    @overload
    def locator(
        self,
        selector: str,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> Locator: ...

    @overload
    def locator(
        self,
        *,
        prompt: str,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> Locator: ...

    def locator(
        self,
        selector: str | None = None,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        **kwargs: Any,
    ) -> Locator:
        """Get a Playwright locator using a CSS selector, AI-powered prompt, or both.

        This method extends Playwright's locator() with AI capabilities. It supports three modes:
        - **Selector-based**: Get locator using CSS selector (standard Playwright behavior)
        - **AI-powered**: Use natural language to describe the element (returns lazy AILocator)
        - **Fallback mode** (default): Try the selector first, fall back to AI if it fails

        The AI-powered locator is lazy - it only calls ai_locate_element when you actually
        use the locator (e.g., when you call .click(), .fill(), etc.). Note that using this
        AI locator lookup with prompt only works for elements you can interact with on the page.

        Args:
            selector: CSS selector for the target element.
            prompt: Natural language description of which element to locate.
            ai: AI behavior mode. Defaults to "fallback" which tries selector first, then AI.
            **kwargs: All Playwright locator parameters (has_text, has, etc.)

        Returns:
            A Playwright Locator object (or AILocator proxy that acts like one).

        Examples:
            ```python
            # Standard Playwright usage - selector only
            download_button = page.locator("#download-btn")
            await download_button.click()

            # AI-powered - prompt only (returns lazy _AILocator)
            download_button = page.locator(prompt='find "download invoices" button')
            await download_button.click()  # AI resolves XPath here

            # Fallback mode - try selector first, use AI if it fails
            download_button = page.locator("#download-btn", prompt='find "download invoices" button')
            await download_button.click()

            # With Playwright parameters
            submit_button = page.locator(prompt="find submit button", has_text="Submit")
            await submit_button.click()
            ```
        """
        if not selector and not prompt:
            raise ValueError("Missing input: pass a selector and/or a prompt.")

        context = skyvern_context.current()
        if context and context.ai_mode_override:
            ai = context.ai_mode_override

        if ai == "fallback":
            if selector and prompt:
                # Try selector first, then AI
                return AILocator(
                    self.page,
                    self._ai,
                    prompt,
                    selector=selector,
                    selector_kwargs=kwargs,
                    try_selector_first=True,
                )

            if selector:
                return self.page.locator(selector, **kwargs)

            if prompt:
                return AILocator(
                    self.page,
                    self._ai,
                    prompt,
                    selector=None,
                    selector_kwargs=kwargs,
                )

        elif ai == "proactive":
            if prompt:
                # Try AI first, then selector
                return AILocator(
                    self.page,
                    self._ai,
                    prompt,
                    selector=selector,
                    selector_kwargs=kwargs,
                    try_selector_first=False,
                )

        if selector:
            return self.page.locator(selector, **kwargs)

        raise ValueError("Selector is required but was not provided")

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


class SafeParameters(dict):
    """Dict subclass that returns None for missing keys instead of raising KeyError.

    Cached scripts generated by the AI reviewer may reference parameter names
    that don't exist in the workflow definition. This prevents runtime crashes —
    the None value is handled downstream by page.fill() which skips the action.
    """

    def __missing__(self, key: str) -> None:
        LOG.warning("Cached script accessed missing parameter key — skipping", key=key)
        return None


class RunContext:
    def __init__(
        self,
        parameters: dict[str, Any],
        page: SkyvernPage,
        generated_parameters: dict[str, Any] | None = None,
        extracted_params: dict[str, str | None] | None = None,
    ) -> None:
        self.original_parameters = parameters
        self.generated_parameters = generated_parameters
        self.parameters = SafeParameters(copy.deepcopy(parameters))
        if generated_parameters:
            # hydrate the generated parameter fields in the run context parameters
            for key, value in generated_parameters.items():
                if key not in self.parameters:
                    self.parameters[key] = value
        self.page = page
        self.trace: list[ActionCall] = []
        # Store actions and results for step output (similar to agent flow)
        self.actions_and_results: list[tuple[Action, list[ActionResult]]] = []
        # Pre-extracted values from applicant context, keyed by canonical category name
        self.extracted_params: dict[str, str | None] = extracted_params or {}
