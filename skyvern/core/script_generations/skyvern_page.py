from __future__ import annotations

import asyncio
import copy
import datetime
import json as _json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, overload

import structlog
from playwright.async_api import Frame, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.config import settings
from skyvern.core.script_generations.fuzzy_matcher import match_option as _match_option
from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi
from skyvern.exceptions import ScriptTerminationException
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import download_file as download_file_from_url
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.library.ai_locator import AILocator
from skyvern.webeye.actions import handler_utils
from skyvern.webeye.actions.action_types import ActionType

if TYPE_CHECKING:
    from skyvern.webeye.actions.actions import Action
    from skyvern.webeye.actions.responses import ActionResult

LOG = structlog.get_logger()

_EXTRACT_FORM_FIELDS_JS: str | None = None


def _get_extract_form_fields_js() -> str:
    """Load the base form field extraction JS (cached after first read)."""
    global _EXTRACT_FORM_FIELDS_JS
    if _EXTRACT_FORM_FIELDS_JS is None:
        js_path = Path(__file__).parent / "extract_form_fields.js"
        _EXTRACT_FORM_FIELDS_JS = js_path.read_text()
    return _EXTRACT_FORM_FIELDS_JS


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
        self._working_frame: Frame | None = None

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

    @property
    def _locator_scope(self) -> Page | Frame:
        """Return the current locator scope: the working iframe if set, otherwise the page.

        Use for element interaction (locator, click, fill). Keep self.page for
        page-level operations (goto, keyboard, url, title, evaluate, reload, content).
        """
        frame = object.__getattribute__(self, "_working_frame")
        if frame is not None:
            return frame
        return object.__getattribute__(self, "page")

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

    async def _wait_for_selector_with_retry(
        self,
        selector: str,
        timeout: float = 5000,
        max_retries: int = 2,
        retry_interval: float = 1.0,
    ) -> Locator:
        """Wait for a CSS selector to match an element in the DOM, retrying on failure.

        When a page action triggers a redirect or slow render (e.g. SSO login flow),
        the next selector may not exist yet.  This method retries the selector lookup
        a few times with short waits, giving the page time to settle before falling
        back to the expensive AI path.

        Only retries when the element is NOT in the DOM (TimeoutError from wait_for).
        Once an element is found, it's returned immediately — no retries on interaction
        failures, which avoids the risk of double-clicking or double-submitting.

        Returns the located element, or raises TimeoutError if all retries fail.
        """
        # Only retry for code_v2 scripts. Code v1 scripts have different
        # execution patterns and haven't been tested with retries.
        ctx = skyvern_context.current()
        if not ctx or ctx.code_version != 2:
            max_retries = 0

        locator = self._locator_scope.locator(selector).first
        for attempt in range(1 + max_retries):
            try:
                # First attempt uses full timeout; retries use a shorter check
                # since we're just waiting for a redirect/render to complete.
                attempt_timeout = timeout if attempt == 0 else min(timeout, 2000)
                await locator.wait_for(state="attached", timeout=attempt_timeout)
                return locator
            except Exception as exc:
                # Only retry on element-not-found (timeout) or navigation errors
                # (execution context destroyed). Non-transient errors (browser
                # crashed, page closed) are re-raised immediately.
                is_transient = isinstance(exc, PlaywrightTimeoutError) or "execution context" in str(exc).lower()
                if attempt < max_retries and is_transient:
                    LOG.info(
                        "Selector not found, retrying after wait",
                        selector=selector[:120],
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        retry_interval=retry_interval,
                    )
                    await asyncio.sleep(retry_interval)
                    # Re-acquire locator in case the DOM was replaced entirely
                    locator = self._locator_scope.locator(selector).first
                else:
                    raise

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
        mode: str | None = None,
        **kwargs: Any,
    ) -> str | None: ...

    @overload
    async def click(
        self,
        *,
        prompt: str,
        ai: str | None = "fallback",
        mode: str | None = None,
        **kwargs: Any,
    ) -> str | None: ...

    @action_wrap(ActionType.CLICK)
    async def click(
        self,
        selector: str | None = None,
        *,
        prompt: str | None = None,
        ai: str | None = "fallback",
        mode: str | None = None,
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
            mode: When ``"direct"``, perform a raw Playwright click with no AI
                fallback or element preparation.  The action is still recorded
                in the DB so it appears in the timeline.
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

            # Raw Playwright click (still recorded in the timeline)
            await page.click('[data-automation-id="nextButton"]', mode="direct")
            ```
        """
        # Direct mode: raw Playwright click, no AI fallback or element prep.
        if mode == "direct":
            if not selector:
                raise ValueError("mode='direct' requires a selector.")
            timeout = kwargs.pop("timeout", settings.BROWSER_ACTION_TIMEOUT_MS)
            locator = self._locator_scope.locator(selector).first
            await locator.click(timeout=timeout, **kwargs)
            return selector

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
            original_selector = selector  # preserve for fallback episode recording
            if selector:
                try:
                    # Retry selector lookup to handle page transitions (redirects,
                    # slow renders) before burning an expensive AI fallback call.
                    locator = await self._wait_for_selector_with_retry(selector, timeout=timeout)
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
                        locator = self._locator_scope.locator(selector).first
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
                    failed_selector=original_selector or "",
                    block_label=self.current_label,
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
            locator = self._locator_scope.locator(selector)
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

        locator = self._locator_scope.locator(selector, **kwargs)
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
        mode: str | None = None,
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
        mode: str | None = None,
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
        mode: str | None = None,
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
            mode: When ``"direct"``, perform a raw Playwright fill with no AI
                fallback or element preparation.  The action is still recorded
                in the DB so it appears in the timeline.
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

            # Raw Playwright fill (still recorded in the timeline)
            await page.fill('input[data-automation-id="email"]', "user@example.com", mode="direct")
            ```
        """

        # Direct mode: raw Playwright fill, no AI fallback or element prep.
        if mode == "direct":
            if not selector:
                raise ValueError("mode='direct' requires a selector.")
            if value is None:
                raise ValueError("mode='direct' requires a value.")
            timeout = kwargs.pop("timeout", settings.BROWSER_ACTION_TIMEOUT_MS)
            locator = self._locator_scope.locator(selector).first
            await locator.fill(value, timeout=timeout, **kwargs)
            return value

        # Backward compatibility
        intention = kwargs.pop("intention", None)
        if intention is not None and prompt is None:
            prompt = intention

        if not selector and not prompt:
            raise ValueError("Missing input: pass a selector and/or a prompt.")

        # Skip fill when value is None (missing parameter) and AI won't generate one.
        # ai='proactive' means the LLM generates the value from the prompt, so None is fine there.
        if value is None and ai != "proactive":
            if prompt:
                LOG.info(
                    "Upgrading to proactive — value is None but prompt provided",
                    selector=selector,
                    prompt=prompt,
                    original_ai=ai,
                )
                ai = "proactive"
            else:
                LOG.info("Skipping fill — value is None and no prompt", selector=selector, prompt=prompt)
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

        Handles widgets like Google Places, autocomplete location fields, and other
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

        # Skip fill when value is None (missing parameter) and AI won't generate one.
        # ai='proactive' means the LLM generates the value from the prompt, so None is fine there.
        if value is None and ai != "proactive":
            if prompt:
                LOG.info(
                    "Upgrading to proactive — value is None but prompt provided",
                    selector=selector,
                    prompt=prompt,
                    original_ai=ai,
                )
                ai = "proactive"
            else:
                LOG.info("Skipping fill_autocomplete — value is None and no prompt", selector=selector, prompt=prompt)
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
        ".autocomplete-dropdown-container div:visible",  # autocomplete location
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
        locator = self._locator_scope.locator(selector).first

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
        else:
            # No close text match — click the first option as best guess
            first = option_locators[0]
            await first.click(timeout=timeout)
            LOG.info(
                "fill_autocomplete: no close text match, clicked first option",
                selector=selector,
                value=value,
            )

        # Wait for the selection to register in the UI
        await asyncio.sleep(0.5)
        return value

    async def _find_autocomplete_options(
        self,
        custom_selector: str | None = None,
    ) -> list[Locator]:
        """Find visible autocomplete dropdown options on the page."""
        selectors_to_try = [custom_selector] if custom_selector else self._AUTOCOMPLETE_OPTION_SELECTORS

        for sel in selectors_to_try:
            try:
                locator = self._locator_scope.locator(sel)
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
            original_selector = selector  # preserve for fallback episode recording
            if selector:
                try:
                    value = await self.get_actual_value(
                        value,
                        totp_identifier=totp_identifier,
                        totp_url=totp_url,
                    )
                    # Retry selector lookup to handle page transitions (redirects,
                    # slow renders) before burning an expensive AI fallback call.
                    locator = await self._wait_for_selector_with_retry(selector, timeout=timeout)
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
                    failed_selector=original_selector or "",
                    block_label=self.current_label,
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

        locator = self._locator_scope.locator(selector).first
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
                    file_path = await download_file_from_url(
                        files,
                        organization_id=context.organization_id if context else None,
                    )
                    locator = self._locator_scope.locator(selector)
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

        file_path = await download_file_from_url(files, organization_id=context.organization_id if context else None)
        locator = self._locator_scope.locator(selector)
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
                    locator = self._locator_scope.locator(selector)
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
            locator = self._locator_scope.locator(selector)
            await locator.select_option(value, timeout=timeout, **kwargs)
        return value

    @action_wrap(ActionType.WAIT)
    async def wait(
        self,
        seconds: float | None = None,
        **kwargs: Any,
    ) -> None:
        timeout_ms = kwargs.pop("timeout_ms", None)
        if seconds is not None:
            await asyncio.sleep(seconds)
        elif timeout_ms is not None:
            await asyncio.sleep(timeout_ms / 1000.0)
        else:
            await asyncio.sleep(0)

    @action_wrap(ActionType.NULL_ACTION)
    async def null_action(self, **kwargs: Any) -> None:
        return

    @action_wrap(ActionType.SOLVE_CAPTCHA)
    async def solve_captcha(self, prompt: str | None = None) -> None:
        raise NotImplementedError("Solve captcha is not supported outside server context")

    @action_wrap(ActionType.TERMINATE)
    async def terminate(self, errors: list[str], **kwargs: Any) -> None:
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

        context = skyvern_context.current()
        file_path = await download_file_from_url(
            download_url,
            filename=file_name,
            organization_id=context.organization_id if context else None,
        )
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
        skip_refresh = kwargs.pop("skip_refresh", False)
        extra_kwargs: dict[str, Any] = {}
        if "system_prompt" in kwargs:
            extra_kwargs["system_prompt"] = kwargs.pop("system_prompt")
        return await self._ai.ai_extract(
            prompt=prompt,
            schema=schema,
            error_code_mapping=error_code_mapping,
            intention=intention,
            data=data,
            skip_refresh=skip_refresh,
            **extra_kwargs,
        )

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

    async def extract_form_fields(self) -> list[dict[str, Any]]:
        """Scan the page for visible form fields using DOM inspection (no LLM).

        The base scanner handles standard HTML form elements (input, select,
        textarea) and ARIA role-based groups.  Platform-specific passes
        (e.g., custom listbox buttons, multiselect widgets) are injected
        at runtime via ``AgentFunction.get_form_field_extraction_js()``.

        Returns a list of field descriptors.
        """
        base_js = _get_extract_form_fields_js()

        ext_js = app.AGENT_FUNCTION.get_form_field_extraction_js(url=self.page.url)
        if ext_js:
            marker = "// PLATFORM_EXTENSION_POINT"
            if marker not in base_js:
                LOG.warning("extract_form_fields: extension point marker missing from base JS")
            else:
                base_js = base_js.replace(marker, ext_js)

        return await self.page.evaluate(base_js)

    async def dynamic_field_map(
        self,
        form_fields: list[dict[str, Any]],
        data: dict[str, Any],
        *,
        prompt: str | None = None,
    ) -> dict[int, str | list | bool | None]:
        """Map data to form fields via a single cheap text-only LLM call.

        One LLM call sees ALL fields + ALL data and produces a complete mapping —
        no deterministic matching, no caching, no accumulated state.

        Args:
            form_fields: Output of :meth:`extract_form_fields`.
            data: Flat dict of data keys/values to map to form fields.

        Returns:
            Mapping of 0-based field index -> value to fill (or None to skip).
        """
        if not form_fields or not data:
            return {}

        # Build field descriptions for the LLM
        field_descs: list[dict[str, Any]] = []
        for field in form_fields:
            label = field.get("label") or field.get("name") or field.get("placeholder") or "unknown"
            field_type = field.get("type", "text")
            options: list[str] | None = None
            if field.get("options"):
                options = [o.get("label") or o.get("value", "") for o in field["options"]]
            desc: dict[str, Any] = {
                "label": label,
                "type": field_type,
                "required": field.get("required", False),
                "placeholder": field.get("placeholder"),
                "options": options,
            }
            if field.get("currentValue"):
                desc["currentValue"] = field["currentValue"]
            if field.get("formatHint"):
                desc["formatHint"] = field["formatHint"]
            field_descs.append(desc)

        prompt_text = prompt_engine.load_prompt(
            template="form-field-mapper",
            form_fields=field_descs,
            data=data,
            prompt=prompt,
            platform_hints=app.AGENT_FUNCTION.get_form_field_mapper_hints(),
        )

        try:
            skyvern_ctx = skyvern_context.current()
            org_id = skyvern_ctx.organization_id if skyvern_ctx else None
            if skyvern_ctx:
                skyvern_ctx.script_llm_call_count += 1

            json_response = await app.SECONDARY_LLM_API_HANDLER(
                prompt=prompt_text,
                prompt_name="form-field-mapper",
                organization_id=org_id,
            )

            if not isinstance(json_response, dict):
                LOG.warning(
                    "dynamic_field_map: LLM returned non-dict",
                    response_type=type(json_response).__name__,
                )
                raise ValueError(f"LLM returned {type(json_response).__name__} instead of dict")

            result: dict[int, Any] = {}
            for k, v in json_response.items():
                if v is None:
                    continue
                try:
                    idx = int(k) - 1  # 1-indexed prompt -> 0-indexed
                    if 0 <= idx < len(form_fields):
                        result[idx] = v
                except (ValueError, TypeError):
                    LOG.warning("dynamic_field_map: non-numeric key in LLM response", key=k)
            mapped_labels = [(form_fields[i].get("label") or form_fields[i].get("name") or "?")[:40] for i in result]
            unmapped_labels = [
                (f.get("label") or f.get("name") or "?")[:40] for idx, f in enumerate(form_fields) if idx not in result
            ]
            LOG.info(
                "dynamic_field_map: mapped fields",
                mapped=len(result),
                total=len(form_fields),
                mapped_labels=mapped_labels,
                unmapped_labels=unmapped_labels,
            )
            return result

        except Exception:
            LOG.warning("dynamic_field_map: LLM call failed", exc_info=True)
            raise

    async def fill_from_mapping(
        self,
        form_fields: list[dict[str, Any]],
        mapping: dict[int, str | list | bool | None],
        data: dict[str, Any] | None = None,
    ) -> None:
        """Fill form fields using a pre-computed mapping from :meth:`dynamic_field_map`.

        Iterates over the mapping and fills each field using the appropriate
        browser method based on field type.  No LLM calls — pure execution.

        Args:
            form_fields: Output of :meth:`extract_form_fields`.
            mapping: Output of :meth:`dynamic_field_map` (index -> value).
            data: Original data dict for post-fill file upload matching.
        """
        ai_fallback_count = 0
        max_ai_fallbacks = 10

        def _budget_available() -> bool:
            nonlocal ai_fallback_count
            if ai_fallback_count >= max_ai_fallbacks:
                LOG.warning("fill_from_mapping: AI fallback budget exhausted", count=ai_fallback_count)
                return False
            ai_fallback_count += 1
            return True

        for idx, value in sorted(mapping.items()):
            if idx >= len(form_fields) or value is None:
                continue

            field = form_fields[idx]
            selector = field.get("selector", "")
            field_type = field.get("type", "text")
            field_tag = field.get("tag", "input")
            label = field.get("label") or field.get("name") or "unknown"

            try:
                if field_type in ("radio_group", "checkbox_group"):
                    LOG.info(
                        "fill_from_mapping: processing group field",
                        field_label=label[:50],
                        field_type=field_type,
                        field_index=idx,
                        value=str(value)[:50],
                        options_count=len(field.get("options", [])),
                        option_labels=[(o.get("label") or "?")[:30] for o in field.get("options", [])],
                    )
                    if isinstance(value, str):
                        try:
                            parsed = _json.loads(value)
                            selected = (
                                [str(v).lower().strip() for v in parsed]
                                if isinstance(parsed, list)
                                else [value.lower().strip()]
                            )
                        except (ValueError, TypeError):
                            selected = [value.lower().strip()]
                    elif isinstance(value, list):
                        selected = [str(v).lower().strip() for v in value]
                    else:
                        selected = [str(value).lower().strip()]

                    options = field.get("options", [])
                    opt_labels = [(o.get("label") or o.get("value", "")).strip() for o in options]

                    matched_any = False
                    for sel_label in selected:
                        match_idx = _match_option(sel_label, opt_labels)
                        if match_idx is not None:
                            await self.click(selector=options[match_idx]["selector"], ai=None)
                            matched_any = True
                            break

                    if not matched_any:
                        # No option text-matched — use AI fallback (Code 2.0 style)
                        LOG.info(
                            "fill_from_mapping: no option matched for group, using AI fallback",
                            field_label=label,
                            intended_value=str(value)[:100],
                            available_options=[o[:50] for o in opt_labels],
                        )
                        if _budget_available():
                            try:
                                self._track_ai_call()
                                prompt = f"For the question '{label}', select the option closest to '{value}'"
                                await self.click(selector=selector, ai="fallback", prompt=prompt)
                            except Exception:
                                LOG.warning(
                                    "fill_from_mapping: AI fallback for radio/checkbox group failed, skipping",
                                    field_label=label,
                                )

                elif field_tag == "select":
                    locator = self._locator_scope.locator(selector)
                    try:
                        await locator.select_option(label=str(value), timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
                    except Exception:
                        try:
                            await locator.select_option(str(value), timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
                        except Exception:
                            # Dropdown value didn't match — AI fallback (Code 2.0 style)
                            LOG.info(
                                "fill_from_mapping: select option not found, using AI fallback",
                                field_label=label,
                                intended_value=str(value)[:100],
                            )
                            if _budget_available():
                                try:
                                    self._track_ai_call()
                                    prompt = f"Select '{value}' from the '{label}' dropdown"
                                    await self.select_option(selector=selector, ai="fallback", prompt=prompt)
                                except Exception:
                                    LOG.warning(
                                        "fill_from_mapping: select AI fallback failed, skipping", field_label=label
                                    )

                elif field_type in ("checkbox", "radio"):
                    if value and str(value).lower() not in ("false", "no", "0", "skip"):
                        await self.click(selector=selector, ai=None)

                elif field_type == "toggle":
                    # Standalone toggles — only click if LLM explicitly said true
                    if value is True or str(value).lower() in ("true", "yes", "1"):
                        await self.click(selector=selector, ai=None)

                elif field_type == "file":
                    await self.upload_file(
                        selector=selector,
                        files=str(value),
                        ai="fallback",
                        prompt=f"Upload file for {label}",
                    )

                elif field_type in ("multiselect", "listbox") or (field.get("placeholder") or "").lower() == "search":
                    # Custom widgets (e.g., multiselect chip-pickers, listbox
                    # dropdowns).  Dispatch to platform-specific filler via
                    # AgentFunction; fall back to element_fallback if no
                    # platform handler is registered or the handler fails.
                    LOG.info(
                        "fill_from_mapping: custom widget detected",
                        field_label=label,
                        widget_type=field_type,
                        value=str(value)[:50],
                    )
                    if _budget_available():
                        filled = await app.AGENT_FUNCTION.fill_custom_widget(
                            self.page,
                            field,
                            value,
                            label,
                        )
                        # None means "not handled" — use element_fallback
                        if filled is None or filled is False:
                            if _budget_available():
                                self._track_ai_call()
                                await self.element_fallback(
                                    navigation_goal=(
                                        f"For the '{label}' field, select or type '{str(value)[:30]}' "
                                        f"and pick the best match. Do NOT click Save and Continue."
                                    ),
                                    max_steps=3,
                                )

                elif field_type in ("search-dropdown", "dropdown"):
                    # Combobox / React Select: click to open, type to filter,
                    # click the matching option.
                    str_value = str(value)
                    locator = self._locator_scope.locator(selector).first
                    await locator.click(timeout=5000)
                    await asyncio.sleep(0.3)
                    await locator.fill("")
                    search_text = str_value.split(",")[0].strip()[:25]
                    await self.page.keyboard.type(search_text, delay=50)
                    await asyncio.sleep(0.5)
                    option = self._locator_scope.locator('[class*="select__option"]:visible').first
                    try:
                        await option.click(timeout=3000)
                    except Exception:
                        await self.page.keyboard.press("Enter")
                    await asyncio.sleep(0.3)

                else:
                    await self.fill(selector=selector, value=str(value), ai=None)

            except Exception:
                LOG.warning(
                    "fill_from_mapping: field fill failed, trying AI fallback",
                    field_label=label,
                    field_type=field_type,
                    field_index=idx,
                    exc_info=True,
                )
                # Field-type-aware AI fallback (Code 2.0 — selector + fallback, not proactive)
                if not _budget_available():
                    continue
                try:
                    self._track_ai_call()
                    if field_type in ("radio_group", "checkbox_group"):
                        prompt = f"Select '{value}' for the question '{label}'"
                        await self.click(selector=selector, ai="fallback", prompt=prompt)
                    elif field_type in ("radio", "checkbox"):
                        prompt = f"Click the '{label}' option to select '{value}'"
                        await self.click(selector=selector, ai="fallback", prompt=prompt)
                    elif field_tag == "select":
                        prompt = f"Select '{value}' from the '{label}' dropdown"
                        await self.select_option(selector=selector, ai="fallback", prompt=prompt)
                    else:
                        prompt = f"Fill the '{label}' field with: {value}"
                        await self.fill(selector=selector, ai="fallback", prompt=prompt)
                except Exception:
                    LOG.warning("fill_from_mapping: AI fallback also failed", field_label=label, exc_info=True)

        # Post-fill: handle unmapped file upload fields by matching URL parameters
        # LLMs often return null for file fields even when a matching URL parameter exists.
        # This catches those cases by scanning for file fields that weren't in the mapping
        # and trying to match them against URL-like parameter values.
        if data:
            # Collect all URL params — including nested ones in user_data JSON
            url_params = {k: v for k, v in data.items() if isinstance(v, str) and v.startswith("http")}
            # Parse user_data if it's a JSON string (resume_url is often nested inside it)
            user_data_str = data.get("user_data", "")
            if isinstance(user_data_str, str) and user_data_str.startswith("{"):
                try:
                    user_data_parsed = _json.loads(user_data_str)
                    for k, v in user_data_parsed.items():
                        if isinstance(v, str) and v.startswith("http") and k not in url_params:
                            url_params[k] = v
                except Exception:
                    pass
            file_fields = [(i, f) for i, f in enumerate(form_fields) if f.get("type") == "file"]
            unmapped_files = [(i, f) for i, f in file_fields if i not in mapping]
            LOG.info(
                "fill_from_mapping: file upload check",
                url_params_count=len(url_params),
                url_param_keys=list(url_params.keys()) if url_params else [],
                file_field_count=len(file_fields),
                unmapped_file_count=len(unmapped_files),
                unmapped_file_labels=[(f.get("label") or f.get("name") or "?")[:50] for _, f in unmapped_files],
            )
            if url_params and unmapped_files:
                uploaded = False
                for idx, field in enumerate(form_fields):
                    if field.get("type") != "file" or idx in mapping:
                        continue
                    field_label = (field.get("label") or "").lower()
                    field_name = (field.get("name") or "").lower()
                    selector = field.get("selector", "")
                    if not selector:
                        continue
                    # Try to match file field name/label against parameter keys
                    for param_key, param_url in url_params.items():
                        pk = param_key.lower()
                        if (
                            pk == field_name  # "resume" == "resume"
                            or (len(pk) >= 3 and pk in field_name)  # "resume" in "resume-upload"
                            or (len(field_name) >= 3 and field_name in pk)  # "doc" in "resume_doc"
                            or (field_label and len(pk) >= 3 and pk in field_label)
                            # Generic file upload fallback — match resume/cv params
                            # only when the field label is also generic or matches
                            or (
                                ("resume" in pk or "cv" in pk)
                                and (
                                    not field_label
                                    or "resume" in field_label
                                    or "cv" in field_label
                                    or "upload" in field_label
                                )
                            )
                        ):
                            LOG.info(
                                "fill_from_mapping: matched URL param to file field",
                                param_key=param_key,
                                field_label=field_label,
                            )
                            try:
                                await self.upload_file(
                                    selector=selector,
                                    files=param_url,
                                    ai="fallback",
                                    prompt=f"Upload resume file to the '{field_label or 'file upload'}' field",
                                )
                                uploaded = True
                            except Exception:
                                LOG.warning(
                                    "fill_from_mapping: file upload failed",
                                    field_label=field_label,
                                    param_url=param_url[:100],
                                    exc_info=True,
                                )
                            break
                    if uploaded:
                        break

    async def validate_mapping(
        self,
        form_fields: list[dict[str, Any]],
        mapping: dict[int, str | list | bool | None],
        prompt: str | None,
    ) -> bool:
        """Validate the field mapping against the user's prompt/instructions.

        Makes one LLM call that sees the prompt (user instructions),
        the form fields, and what was mapped to each field.  Returns True if
        the run should complete, False if it should terminate.

        This catches user directives like "terminate if you can't answer the
        security clearance question" or "never fabricate answers — fail if
        data is missing for required fields."

        Args:
            form_fields: Output of :meth:`extract_form_fields`.
            mapping: Output of :meth:`dynamic_field_map`.
            prompt: The user's instructions/prompt for this automation.

        Returns:
            True to complete, False to terminate.
        """
        if not prompt:
            return True

        # Build a summary of what was mapped
        field_summary: list[str] = []
        for i, field in enumerate(form_fields):
            label = field.get("label") or field.get("name") or f"field_{i}"
            field_type = field.get("type", "text")
            required = field.get("required", False)
            value = mapping.get(i)
            if value is not None:
                field_summary.append(f"- {label} ({field_type}{'*' if required else ''}): {str(value)[:100]}")
            else:
                field_summary.append(f"- {label} ({field_type}{'*' if required else ''}): [NOT FILLED]")

        prompt_text = (
            "You are validating a job application form that was filled automatically.\n\n"
            "# User Instructions\n"
            f"```\n{prompt}\n```\n\n"
            "# Form Fields and Values\n" + "\n".join(field_summary) + "\n\n"
            "# Task\n"
            "Review the filled values against the user instructions above.\n"
            "Decide whether this application should COMPLETE or TERMINATE.\n\n"
            "TERMINATE only if:\n"
            "- The user instructions EXPLICITLY say to terminate/fail/stop for a specific condition, "
            "and that condition is met (e.g., 'terminate if work authorization is unknown')\n"
            "- The user instructions say 'do not submit', 'don't submit', 'don't click submit', "
            "or similar — this means they are testing and want to stop before submission\n"
            "- Do NOT terminate just because some fields are [NOT FILLED] — that's normal for "
            "optional fields or file uploads without matching data\n\n"
            "COMPLETE if:\n"
            "- The user didn't specify any termination conditions (DEFAULT — most cases)\n"
            "- All explicit user termination conditions are satisfied\n"
            "- Fields are filled reasonably given the available data\n"
            "- Some fields being [NOT FILLED] is OK as long as no user instruction says otherwise\n\n"
            "# Output\n"
            'Return JSON: {"decision": "complete"} or {"decision": "terminate", "reason": "brief explanation"}\n'
        )

        try:
            skyvern_ctx = skyvern_context.current()
            org_id = skyvern_ctx.organization_id if skyvern_ctx else None
            if skyvern_ctx:
                skyvern_ctx.script_llm_call_count += 1

            result = await app.SECONDARY_LLM_API_HANDLER(
                prompt=prompt_text,
                prompt_name="form-validate-mapping",
                organization_id=org_id,
            )

            decision = result.get("decision", "complete") if isinstance(result, dict) else "complete"

            if decision == "terminate":
                reason = result.get("reason", "Validation failed") if isinstance(result, dict) else "Validation failed"
                LOG.info(
                    "validate_mapping: TERMINATE",
                    reason=reason,
                    prompt=prompt[:200],
                )
                return False

            LOG.info("validate_mapping: COMPLETE")
            return True

        except Exception:
            LOG.warning("validate_mapping: validation call failed, defaulting to complete", exc_info=True)
            return True

    async def fill_form(
        self,
        data: dict[str, Any],
        *,
        prompt: str = "Fill out the form",
    ) -> None:
        """Scan page for form fields, map data to fields via LLM, and fill them.

        This is the primary SDK interface for form filling. It composes:
        1. extract_form_fields() — scan all fields from the DOM (free)
        2. dynamic_field_map() — one LLM call to map data to fields
        3. validate_mapping() — one LLM call to check user conditions
        4. fill_from_mapping() — fill via CSS selectors with AI fallback

        Args:
            data: Dict of data keys/values to fill into the form.
            prompt: User instructions for how to fill the form.
        """
        form_fields = await self.extract_form_fields()

        LOG.info(
            "fill_form: extracted fields",
            field_count=len(form_fields),
            data_keys=list(data.keys())[:10],
        )

        if not form_fields:
            raise RuntimeError(
                "fill_form found 0 form fields on the page. "
                "The page may not have finished rendering — try adding "
                "await page.wait(timeout_ms=5000) before fill_form()."
            )

        mapping = await self.dynamic_field_map(form_fields, data, prompt=prompt)

        if not await self.validate_mapping(form_fields, mapping, prompt):
            raise ScriptTerminationException("fill_form validation failed: user termination conditions not met")

        await self.fill_from_mapping(form_fields, mapping, data=data)

    async def _dump_html(self, debug_dir: str | None, label: str) -> None:
        """Dump current page HTML to a timestamped file for debugging."""
        if not debug_dir:
            return
        try:
            ts = datetime.datetime.now().strftime("%H%M%S_%f")[:-3]
            filename = f"{ts}_{label}.html"
            filepath = os.path.join(debug_dir, filename)
            html = await self.page.content()
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(html)
            LOG.info("_dump_html: saved", path=filepath, size=len(html))
        except Exception:
            LOG.warning("_dump_html: failed", exc_info=True)

    async def fill_multipage_form(
        self,
        data: dict[str, Any],
        *,
        prompt: str = "Fill out the form",
        next_button: str = 'button:has-text("Save and Continue"), button:has-text("Next"), button:has-text("Continue")',
        max_pages: int = 10,
        timeout_seconds: float = 300,
        debug_dir: str | None = None,
    ) -> int:
        """Fill a multi-page form by looping: fill current page → click next → repeat.

        Returns the number of pages filled. Stops when:
        - No fillable form fields are found on the current page (e.g., Review page)
        - The next button is not found (last page)
        - max_pages is reached
        - Wall-clock timeout is exceeded

        Args:
            data: Dict of data keys/values to fill into the form.
            prompt: User instructions for how to fill the form.
            next_button: CSS selector(s) for the next/continue button.
            max_pages: Safety limit on number of pages to fill.
            timeout_seconds: Wall-clock timeout for the entire multi-page fill (default 5 min).
        """
        start_time = time.monotonic()
        pages_filled = 0
        prev_field_signature: str | None = None
        consecutive_validation_failures = 0

        for page_num in range(max_pages):
            elapsed = time.monotonic() - start_time
            if elapsed > timeout_seconds:
                LOG.warning(
                    "fill_multipage_form: timeout exceeded, stopping",
                    page_num=page_num,
                    elapsed_s=round(elapsed, 1),
                    timeout_s=timeout_seconds,
                )
                break

            # Small wait on page 1+ to let React DOM settle after transition
            if page_num > 0:
                await asyncio.sleep(1)

            form_fields = await self.extract_form_fields()

            # Filter to fillable fields — includes standard inputs AND custom widgets
            fillable = [
                f
                for f in form_fields
                if (f.get("tag") in ("input", "select", "textarea") and f.get("type") != "hidden")
                or f.get("type") in ("listbox", "multiselect", "toggle")
            ]

            if not fillable:
                await self._dump_html(debug_dir, f"p{page_num}_empty")
                LOG.info(
                    "fill_multipage_form: no fillable fields on page, stopping",
                    page_num=page_num,
                    total_fields=len(form_fields),
                )
                break

            # Detect stuck on same page: if field labels haven't changed, the
            # next-button click didn't navigate. Stop to avoid infinite loop.
            field_sig = "|".join((f.get("label") or f.get("name") or f.get("placeholder") or "") for f in fillable)
            if field_sig and field_sig == prev_field_signature:
                LOG.warning(
                    "fill_multipage_form: same fields detected, page did not advance — stopping",
                    page_num=page_num,
                    field_count=len(fillable),
                )
                break
            prev_field_signature = field_sig

            field_labels = [(f.get("label") or f.get("name") or f.get("placeholder") or "?")[:40] for f in fillable]
            # Log unlabeled fields with their raw data for debugging
            unlabeled = [
                {k: v for k, v in f.items() if k in ("tag", "type", "selector", "placeholder", "name")}
                for f in fillable
                if not f.get("label") and not f.get("name") and not f.get("placeholder")
            ]
            LOG.info(
                "fill_multipage_form: filling page",
                page_num=page_num,
                field_count=len(fillable),
                field_labels=field_labels,
                unlabeled_fields=unlabeled[:5] if unlabeled else None,
                elapsed_s=round(time.monotonic() - start_time, 1),
            )

            await self._dump_html(debug_dir, f"p{page_num}_00_before_fill")

            mapping = await self.dynamic_field_map(form_fields, data, prompt=prompt)

            # Skip validation on intermediate pages — validate_mapping checks user
            # instructions like "do not submit" which only apply to the final page.
            # We'll validate after the loop if needed.
            await self.fill_from_mapping(form_fields, mapping, data=data)
            pages_filled += 1

            await self._dump_html(debug_dir, f"p{page_num}_01_after_fill")

            # Re-scan for dynamically revealed fields (e.g., State appears after
            # Country may be auto-filled). Fill any new fields that appeared.
            rescan_fields = await self.extract_form_fields()
            rescan_fillable = [
                f
                for f in rescan_fields
                if (f.get("tag") in ("input", "select", "textarea") and f.get("type") != "hidden")
                or f.get("type") in ("listbox", "multiselect", "toggle")
            ]
            new_field_count = len(rescan_fillable) - len(fillable)
            if new_field_count > 0:
                LOG.info(
                    "fill_multipage_form: new fields appeared after fill, re-mapping",
                    page_num=page_num,
                    original_count=len(fillable),
                    new_count=len(rescan_fillable),
                    new_fields=new_field_count,
                )
                rescan_mapping = await self.dynamic_field_map(rescan_fields, data, prompt=prompt)
                # Only fill indices that weren't in the original mapping
                new_mapping: dict[int, str | list | bool | None] = {
                    k: v for k, v in rescan_mapping.items() if k not in mapping and v is not None
                }
                if new_mapping:
                    await self.fill_from_mapping(rescan_fields, new_mapping, data=data)
                    await self._dump_html(debug_dir, f"p{page_num}_02_after_rescan_fill")
                # Update field signature to use the new fields for stuck detection
                fillable = rescan_fillable
                form_fields = rescan_fields
                prev_field_signature = "|".join(
                    (f.get("label") or f.get("name") or f.get("placeholder") or "") for f in fillable
                )

            # Try to click the next/continue button
            try:
                await self.click(
                    selector=next_button,
                    ai="fallback",
                    prompt="Click the button to save and continue to the next page of the application",
                )
            except Exception:
                LOG.info(
                    "fill_multipage_form: next button not found, stopping",
                    page_num=page_num,
                )
                break

            await self._dump_html(debug_dir, f"p{page_num}_03_after_click_next")

            # Wait for page transition using DOM readiness check
            try:
                from skyvern.webeye.utils.page import SkyvernFrame

                skyvern_frame = await SkyvernFrame.create_instance(frame=self.page)
                await skyvern_frame.wait_for_page_ready(
                    network_idle_timeout_ms=3000,
                    loading_indicator_timeout_ms=5000,
                    dom_stable_ms=300,
                    dom_stability_timeout_ms=3000,
                )
            except Exception:
                # Fall back to a short sleep if readiness check fails
                await asyncio.sleep(2)

            # Check for validation errors AFTER clicking next (some sites show
            # errors only after clicking Save and Continue)
            try:
                post_click_errors = await self.page.evaluate("""() => {
                    const errs = [];
                    // Common error patterns: inline error messages, alert banners
                    const selectors = '[data-automation-id*="error"], [data-automation-id*="Error"], [class*="errorMessage"], [class*="fieldError"], [role="alert"], [aria-invalid="true"]';
                    document.querySelectorAll(selectors).forEach(el => {
                        const t = el.textContent.trim();
                        if (t && t.length > 3 && t.length < 300 && el.offsetWidth > 0) {
                            // Skip upload success messages — not real errors
                            if (/successfully uploaded/i.test(t)) return;
                            errs.push(t.substring(0, 100));
                        }
                    });
                    return errs;
                }""")
                if post_click_errors:
                    consecutive_validation_failures += 1
                    LOG.warning(
                        "fill_multipage_form: validation errors after Save and Continue",
                        page_num=page_num,
                        error_count=len(post_click_errors),
                        errors=post_click_errors[:5],
                        consecutive_failures=consecutive_validation_failures,
                    )
                    await self._dump_html(debug_dir, f"p{page_num}_04_validation_errors")
                    if consecutive_validation_failures >= 3:
                        LOG.warning(
                            "fill_multipage_form: too many consecutive validation failures, stopping",
                            page_num=page_num,
                            failures=consecutive_validation_failures,
                        )
                        break
                else:
                    consecutive_validation_failures = 0
            except Exception:
                pass

        elapsed = time.monotonic() - start_time
        LOG.info(
            "fill_multipage_form: completed",
            pages_filled=pages_filled,
            total_elapsed_s=round(elapsed, 1),
        )
        return pages_filled

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

        # Priority 3: Canonical category match (cloud-only; returns None in OSS)
        category = app.AGENT_FUNCTION.match_field_to_canonical_category(field_label)
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
            cat_obj = app.AGENT_FUNCTION.get_canonical_category(category_name)
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
                locator = self._locator_scope.locator(selector)
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
                        locator = self._locator_scope.locator(alt_selector)
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
                actual = await self._locator_scope.locator(selector).input_value(timeout=2000)
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

            # Check for visible error messages.
            # NOTE: When a frame is active, this only detects errors inside that
            # frame (e.g. payment form errors). Main-page error badges are not
            # visible from within an iframe — this is intentional for frame-scoped
            # validation but callers should be aware of the scoping.
            error_count = await self._locator_scope.locator(
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
        max_steps: int = 5,
    ) -> None:
        """Activate the AI agent from the CURRENT page position to achieve a goal.

        Instead of re-running the entire block when cached code encounters an unknown
        state, this method activates the AI agent from the current page position.
        Much cheaper than a full block re-execution.

        Args:
            navigation_goal: The goal for the AI agent to achieve from the current page.
            max_steps: Maximum number of agent steps before giving up. Defaults to 5.

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
                return self._locator_scope.locator(selector, **kwargs)

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
            return self._locator_scope.locator(selector, **kwargs)

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
        await EventStrategyFactory.move_cursor(self.page, x, y)

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

    @property
    def prompt(self) -> str | None:
        """Return the per-iteration prompt from SkyvernContext (set by script_service per loop iteration)."""
        ctx = skyvern_context.current()
        return ctx.prompt if ctx else None

    @property
    def loop_value(self) -> Any | None:
        """Return the current loop iteration value from SkyvernContext.loop_metadata."""
        ctx = skyvern_context.current()
        if ctx and ctx.loop_metadata:
            return ctx.loop_metadata.get("current_value")
        return None

    def loop_item_selector(self) -> str | None:
        """Build a CSS selector to click the current loop item's link on the page.

        Strategies in order of reliability:
        1. URL path matching — extract the last meaningful path segment from URL
           values and match via ``a[href*="path-segment"]``. Skips bare domains
           (no path) to avoid matching every link on the page.
        2. Text matching — use the longest non-URL text value via
           ``a:has-text("title")``.

        Works for both navigation clicks and file downloads. Returns None when
        no viable selector can be built (caller should fall back to AI).
        """
        value = self.loop_value
        if not value or not isinstance(value, dict):
            return None

        texts: list[str] = []
        for v in value.values():
            if not isinstance(v, str) or not v.strip():
                continue

            # Strategy 1: URL values → href selector from path segment.
            # Uses the first URL with a viable path (dict insertion order is
            # stable in Python 3.7+; extraction blocks control field ordering).
            if re.match(r"https?://", v) or v.startswith("/"):
                # Extract path, strip trailing slash
                # "https://example.com/pub/water-act-2014/" → "/pub/water-act-2014"
                # "/files/report.pdf?v=2" → "/files/report.pdf"
                path = re.sub(r"https?://[^/]*", "", v).split("?")[0].split("#")[0].rstrip("/")
                if path and path != "/":
                    segment = path.rsplit("/", 1)[-1]
                    segment = re.sub(r'["\[\]\\]', "", segment)
                    if segment and len(segment) >= 3:
                        return f'a[href*="{segment}"]'
                # Short or empty path segment — fall through to text matching
                continue

            texts.append(v.strip())

        if not texts:
            return None

        # Strategy 2: Direct link text match — many sites make the document
        # title clickable (e.g., <a href="...">Annual Report 2025</a>).
        longest = max(texts, key=len)
        escaped = longest.replace("\n", " ").replace("\r", "").replace('"', '\\"')
        if len(escaped) >= 3:
            return f'a:has-text("{escaped}")'

        return None

    # Backward-compatible alias — existing cached scripts reference download_selector()
    def download_selector(self) -> str | None:
        return self.loop_item_selector()
