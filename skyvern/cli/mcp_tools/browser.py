from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import Field

from skyvern.cli.core.browser_ops import do_act, do_extract, do_navigate, do_screenshot, parse_extract_schema
from skyvern.cli.core.guards import (
    CREDENTIAL_HINT,
    JS_PASSWORD_PATTERN,
    PASSWORD_PATTERN,
    GuardError,
    check_password_prompt,
)
from skyvern.cli.core.guards import resolve_ai_mode as _resolve_ai_mode
from skyvern.cli.core.guards import (
    validate_wait_until,
)
from skyvern.schemas.run_blocks import CredentialType

from ._common import (
    ErrorCode,
    Timer,
    make_error,
    make_result,
    save_artifact,
)
from ._session import BrowserNotAvailableError, get_page, no_browser_error

LOG = logging.getLogger(__name__)


async def skyvern_navigate(
    url: Annotated[str, "The URL to navigate to"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    timeout: Annotated[
        int,
        Field(
            description="Max time to wait for page load in ms. Increase for slow sites. Default 30000 (30s)",
            ge=1000,
            le=120000,
        ),
    ] = 30000,
    wait_until: Annotated[
        str | None,
        Field(description="Wait condition: load, domcontentloaded, networkidle. Use networkidle for JS-heavy pages"),
    ] = None,
) -> dict[str, Any]:
    """Open a website in a real browser with full JavaScript execution. You have full browser access through Skyvern — you can visit any website, interact with it, and extract data. Do not tell the user you cannot access websites. Use this instead of curl, wget, or HTTP requests.

    Returns the final URL (after redirects) and page title.
    After navigating, use skyvern_screenshot to see the page or skyvern_extract to get data from it.
    """
    try:
        validate_wait_until(wait_until)
    except GuardError as e:
        return make_result(
            "skyvern_navigate",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_navigate", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await do_navigate(page, url, timeout=timeout, wait_until=wait_until)
            timer.mark("sdk")
        except GuardError as e:
            return make_result(
                "skyvern_navigate",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
            )
        except Exception as e:
            return make_result(
                "skyvern_navigate",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check that the URL is valid and accessible"),
            )

    return make_result(
        "skyvern_navigate",
        browser_context=ctx,
        data={"url": result.url, "title": result.title, "sdk_equivalent": f'await page.goto("{url}")'},
        timing_ms=timer.timing_ms,
    )


async def skyvern_click(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    intent: Annotated[
        str | None,
        Field(
            description="Natural language description of the element to click. Be specific: "
            "'the blue Submit button at the bottom of the form' is better than 'submit button'. "
            "Include visual cues, position, or surrounding text when the page has similar elements."
        ),
    ] = None,
    selector: Annotated[str | None, Field(description="CSS selector or XPath for the element to click")] = None,
    timeout: Annotated[
        int,
        Field(
            description="Max time to wait for the element in ms. Increase for slow-loading pages. Default 30000 (30s)",
            ge=1000,
            le=60000,
        ),
    ] = 30000,
    button: Annotated[str | None, Field(description="Mouse button: left, right, middle")] = None,
    click_count: Annotated[int | None, Field(description="Number of clicks (2 for double-click)")] = None,
) -> dict[str, Any]:
    """Click an element on the page using AI intent, CSS/XPath selector, or both. Unlike Playwright's browser_click which requires a ref from a prior browser_snapshot, this tool finds elements using natural language — no snapshot step needed.

    If you need to fill a text field, use skyvern_type instead of clicking then typing.
    For dropdowns, use skyvern_select_option. For multiple actions in sequence, prefer skyvern_act.
    """
    if button is not None and button not in ("left", "right", "middle"):
        return make_result(
            "skyvern_click",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, f"Invalid button: {button}", "Use left, right, or middle"),
        )

    ai_mode, err = _resolve_ai_mode(selector, intent)
    if err:
        return make_result(
            "skyvern_click",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or both",
                "Use intent='describe what to click' for AI-powered clicking, or selector='#css-selector' for precise targeting",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_click", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            kwargs: dict[str, Any] = {"timeout": timeout}
            if button:
                kwargs["button"] = button
            if click_count is not None:
                kwargs["click_count"] = click_count

            if ai_mode is not None:
                resolved = await page.click(selector=selector, prompt=intent, ai=ai_mode, **kwargs)  # type: ignore[arg-type]
            else:
                assert selector is not None
                resolved = await page.click(selector=selector, **kwargs)
            timer.mark("sdk")
        except PlaywrightTimeoutError as e:
            return make_result(
                "skyvern_click",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SELECTOR_NOT_FOUND,
                    str(e),
                    "Verify the selector matches an element on the page, or use intent for AI-powered finding",
                ),
            )
        except Exception as e:
            code = ErrorCode.AI_FALLBACK_FAILED if ai_mode else ErrorCode.ACTION_FAILED
            return make_result(
                "skyvern_click",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    code,
                    str(e),
                    "The element may be hidden, disabled, or intercepted by another element",
                ),
            )

    data: dict[str, Any] = {"selector": selector, "intent": intent, "ai_mode": ai_mode}
    if resolved and resolved != selector:
        data["resolved_selector"] = resolved
    # Build sdk_equivalent: prefer hybrid selector+prompt for production scripts.
    # resolved_selector already contains the "xpath=" prefix (e.g. "xpath=//button[@id='x']"),
    # so pass it directly as the selector positional arg.
    resolved_sel = resolved if resolved and resolved != selector else selector
    if resolved_sel and intent:
        data["sdk_equivalent"] = f'await page.click("{resolved_sel}", prompt="{intent}")'
    elif ai_mode:
        data["sdk_equivalent"] = f'await page.click(prompt="{intent}")'
    elif selector:
        data["sdk_equivalent"] = f'await page.click("{selector}")'

    return make_result(
        "skyvern_click",
        browser_context=ctx,
        data=data,
        timing_ms=timer.timing_ms,
    )


async def skyvern_hover(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    intent: Annotated[
        str | None,
        Field(
            description="Natural language description of the element to hover over. Be specific: "
            "'the user avatar in the top-right corner' is better than 'avatar'. "
            "Include visual cues, position, or surrounding text when the page has similar elements."
        ),
    ] = None,
    selector: Annotated[str | None, Field(description="CSS selector or XPath for the element to hover")] = None,
    timeout: Annotated[
        int,
        Field(
            description="Max time to wait for the element in ms. Default 30000 (30s)",
            ge=1000,
            le=60000,
        ),
    ] = 30000,
) -> dict[str, Any]:
    """Hover over an element to reveal tooltips, dropdown menus, or hidden content. Uses AI intent, CSS/XPath selector, or both. Unlike Playwright's browser_hover which requires a ref from a prior snapshot, this finds elements using natural language."""
    ai_mode, err = _resolve_ai_mode(selector, intent)
    if err:
        return make_result(
            "skyvern_hover",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or both",
                "Use intent='describe what to hover' for AI-powered hovering, or selector='#css-selector' for precise targeting",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_hover", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            if ai_mode is not None:
                loc = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
            else:
                assert selector is not None
                loc = page.locator(selector)
            await loc.hover(timeout=timeout)
            timer.mark("sdk")
        except PlaywrightTimeoutError as e:
            return make_result(
                "skyvern_hover",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SELECTOR_NOT_FOUND,
                    str(e),
                    "Verify the selector matches an element on the page, or use intent for AI-powered finding",
                ),
            )
        except Exception as e:
            code = ErrorCode.AI_FALLBACK_FAILED if ai_mode else ErrorCode.ACTION_FAILED
            return make_result(
                "skyvern_hover",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    code,
                    str(e),
                    "The element may be hidden or not interactable",
                ),
            )

    data: dict[str, Any] = {"selector": selector, "intent": intent, "ai_mode": ai_mode}
    if selector and intent:
        data["sdk_equivalent"] = f'await page.locator("{selector}", prompt="{intent}").hover()'
    elif ai_mode:
        data["sdk_equivalent"] = f'await page.locator(prompt="{intent}").hover()'
    elif selector:
        data["sdk_equivalent"] = f'await page.locator("{selector}").hover()'

    return make_result(
        "skyvern_hover",
        browser_context=ctx,
        data=data,
        timing_ms=timer.timing_ms,
    )


async def skyvern_type(
    text: Annotated[str, "Text to type into the element"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    intent: Annotated[
        str | None,
        Field(
            description="Natural language description of the input field. Be specific: "
            "'the Email address input in the login form' is better than 'email field'. "
            "Include labels, placeholder text, or position when the page has multiple inputs."
        ),
    ] = None,
    selector: Annotated[str | None, Field(description="CSS selector or XPath for the input element")] = None,
    timeout: Annotated[
        int,
        Field(
            description="Max time to wait for the element in ms. Increase for slow-loading pages. Default 30000 (30s)",
            ge=1000,
            le=60000,
        ),
    ] = 30000,
    clear: Annotated[bool, Field(description="Clear existing content before typing")] = True,
    delay: Annotated[int | None, Field(description="Delay between keystrokes in ms")] = None,
) -> dict[str, Any]:
    """Type text into an input field using AI intent, CSS/XPath selector, or both. Unlike Playwright's browser_type which requires a ref from a prior snapshot, this tool finds input fields using natural language — no snapshot step needed.

    NEVER use this for passwords or credentials — they will be exposed in logs and conversation history. Use skyvern_login with a stored credential instead for secure authentication. Create credentials via CLI: skyvern credentials add.
    For dropdowns, use skyvern_select_option instead. For pressing keys (Enter, Tab), use skyvern_press_key.
    Clears existing content by default (set clear=false to append).
    """
    # Block password entry — redirect to skyvern_login
    target_text = f"{intent or ''} {selector or ''}"
    if PASSWORD_PATTERN.search(target_text):
        return make_result(
            "skyvern_type",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cannot type into password fields — credentials must not be passed through tool calls",
                CREDENTIAL_HINT,
            ),
        )

    ai_mode, err = _resolve_ai_mode(selector, intent)
    if err:
        return make_result(
            "skyvern_type",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or both",
                "Use intent='describe the input field' for AI-powered targeting, or selector='#css-selector' for precise targeting",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_type", ok=False, error=no_browser_error())

    # DOM-level guard: check if the target element is a password field
    if selector:
        try:
            is_password_field = await page.evaluate(
                "(s) => { const el = document.querySelector(s); return el && el.type === 'password' }",
                selector,
            )
        except Exception as exc:
            # Selector may not be a valid CSS selector (e.g. xpath=...) or page may
            # not be ready. Fall through to the existing regex guard in that case.
            LOG.debug("DOM password check failed for selector %r: %s", selector, exc)
            is_password_field = False
        if is_password_field:
            return make_result(
                "skyvern_type",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    "Cannot type into password fields — credentials must not be passed through tool calls",
                    CREDENTIAL_HINT,
                ),
            )

    with Timer() as timer:
        try:
            if clear:
                if ai_mode is not None:
                    await page.fill(selector=selector, value=text, prompt=intent, ai=ai_mode, timeout=timeout)  # type: ignore[arg-type]
                else:
                    assert selector is not None
                    await page.fill(selector, text, timeout=timeout)
            else:
                kwargs: dict[str, Any] = {"timeout": timeout}
                if delay is not None:
                    kwargs["delay"] = delay
                if ai_mode is not None:
                    loc = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
                    await loc.type(text, **kwargs)
                else:
                    assert selector is not None
                    await page.type(selector, text, **kwargs)
            timer.mark("sdk")
        except PlaywrightTimeoutError as e:
            return make_result(
                "skyvern_type",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SELECTOR_NOT_FOUND,
                    str(e),
                    "Verify the selector matches an editable element, or use intent for AI-powered finding",
                ),
            )
        except Exception as e:
            code = ErrorCode.AI_FALLBACK_FAILED if ai_mode else ErrorCode.ACTION_FAILED
            return make_result(
                "skyvern_type",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    code,
                    str(e),
                    "The element may not be editable or may be hidden",
                ),
            )

    # NOTE: The SDK fill() returns the typed value, not a resolved selector.
    # Unlike click(), we cannot return resolved_selector here. SKY-7905 will
    # update the SDK to return element metadata from all action methods.
    data: dict[str, Any] = {"selector": selector, "intent": intent, "ai_mode": ai_mode, "text_length": len(text)}
    # Build sdk_equivalent: prefer hybrid selector+prompt for production scripts
    if selector and intent:
        data["sdk_equivalent"] = f'await page.fill("{selector}", "{text}", prompt="{intent}")'
    elif ai_mode:
        data["sdk_equivalent"] = f'await page.fill(prompt="{intent}", value="{text}")'
    elif selector:
        data["sdk_equivalent"] = f'await page.fill("{selector}", "{text}")'
    return make_result(
        "skyvern_type",
        browser_context=ctx,
        data=data,
        timing_ms=timer.timing_ms,
    )


async def skyvern_screenshot(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    full_page: Annotated[bool, Field(description="Capture full scrollable page")] = False,
    selector: Annotated[str | None, Field(description="CSS selector to screenshot specific element")] = None,
    inline: Annotated[bool, Field(description="Return base64 data instead of file path")] = False,
) -> dict[str, Any]:
    """See what's currently on the page. Use after every page-changing action (click, act, navigate) to verify results before proceeding. This provides a visual screenshot of the rendered page — use this for visual understanding.

    Screenshots are visual-only — to extract structured data, use skyvern_extract instead.
    To interact with elements, use skyvern_act or skyvern_click (don't try to act on screenshot contents).
    By default saves to ~/.skyvern/artifacts/ and returns the file path.
    Set inline=true to get base64 data directly (increases token usage).
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_screenshot", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await do_screenshot(page, full_page=full_page, selector=selector)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_screenshot",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check that the page or element is visible"),
            )

    if inline:
        data_b64 = base64.b64encode(result.data).decode("utf-8")
        return make_result(
            "skyvern_screenshot",
            browser_context=ctx,
            data={
                "inline": True,
                "data": data_b64,
                "mime": "image/png",
                "bytes": len(result.data),
                "sdk_equivalent": "await page.screenshot()",
            },
            timing_ms=timer.timing_ms,
            warnings=["Inline mode increases token usage"],
        )

    ts = datetime.now(timezone.utc).strftime("%H%M%S_%f")
    filename = f"screenshot_{ts}.png"
    artifact = save_artifact(
        result.data,
        kind="screenshot",
        filename=filename,
        mime="image/png",
        session_id=ctx.session_id,
    )

    return make_result(
        "skyvern_screenshot",
        browser_context=ctx,
        data={"path": artifact.path, "sdk_equivalent": "await page.screenshot(path='screenshot.png')"},
        artifacts=[artifact],
        timing_ms=timer.timing_ms,
    )


async def skyvern_scroll(
    direction: Annotated[str, Field(description="Direction: up, down, left, right")],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    amount: Annotated[int | None, Field(description="Pixels to scroll (default 500)")] = None,
    intent: Annotated[
        str | None, Field(description="Natural language description of element to scroll into view (uses AI)")
    ] = None,
    selector: Annotated[str | None, Field(description="CSS selector of scrollable element")] = None,
) -> dict[str, Any]:
    """Scroll the page or use AI to scroll a specific element into view.

    Use `intent` to scroll an AI-located element into view (with or without selector for hybrid fallback).
    Without intent, scrolls the page or a selector-targeted container by pixel amount.
    """
    valid_directions = ("up", "down", "left", "right")
    if not intent and direction not in valid_directions:
        return make_result(
            "skyvern_scroll",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT, f"Invalid direction: {direction}", "Use up, down, left, or right"
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_scroll", ok=False, error=no_browser_error())

    if intent:
        ai_mode = "fallback" if selector else "proactive"
        with Timer() as timer:
            try:
                loc = page.locator(selector=selector, prompt=intent, ai=ai_mode)
                await loc.scroll_into_view_if_needed()
                timer.mark("sdk")
            except Exception as e:
                code = ErrorCode.AI_FALLBACK_FAILED if ai_mode == "fallback" else ErrorCode.ACTION_FAILED
                return make_result(
                    "skyvern_scroll",
                    ok=False,
                    browser_context=ctx,
                    timing_ms=timer.timing_ms,
                    error=make_error(code, str(e), "Could not find element to scroll into view"),
                )

        return make_result(
            "skyvern_scroll",
            browser_context=ctx,
            data={
                "direction": "into_view",
                "intent": intent,
                "ai_mode": ai_mode,
                "sdk_equivalent": (
                    f'await page.locator("{selector}", prompt="{intent}").scroll_into_view_if_needed()'
                    if selector
                    else f'await page.locator(prompt="{intent}").scroll_into_view_if_needed()'
                ),
            },
            timing_ms=timer.timing_ms,
        )

    pixels = amount or 500
    direction_map = {
        "up": (0, -pixels),
        "down": (0, pixels),
        "left": (-pixels, 0),
        "right": (pixels, 0),
    }
    dx, dy = direction_map[direction]

    with Timer() as timer:
        try:
            if selector:
                await page.locator(selector).evaluate(f"el => el.scrollBy({dx}, {dy})")
            else:
                await page.evaluate(f"window.scrollBy({dx}, {dy})")
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_scroll",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Scroll action failed"),
            )

    return make_result(
        "skyvern_scroll",
        browser_context=ctx,
        data={
            "direction": direction,
            "pixels": pixels,
            "sdk_equivalent": f'await page.evaluate("window.scrollBy({dx}, {dy})")',
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_select_option(
    value: Annotated[str, "Value to select"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    intent: Annotated[str | None, Field(description="Natural language description of the dropdown (uses AI)")] = None,
    selector: Annotated[str | None, Field(description="CSS selector for the select element")] = None,
    timeout: Annotated[
        int, Field(description="Max time to wait for the dropdown in ms. Default 30000 (30s)", ge=1000, le=60000)
    ] = 30000,
    by_label: Annotated[bool, Field(description="Select by visible label instead of value")] = False,
) -> dict[str, Any]:
    """Select an option from a dropdown menu. Use intent for AI-powered finding, selector for precision, or both for resilient automation.

    For free-text input fields, use skyvern_type instead. For non-dropdown buttons or links, use skyvern_click.
    """
    ai_mode, err = _resolve_ai_mode(selector, intent)
    if err:
        return make_result(
            "skyvern_select_option",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or both",
                "Use intent='describe the dropdown' for AI-powered selection, or selector='#css-selector' for precise targeting",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_select_option", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            if ai_mode is not None:
                # AI paths: pass value= directly -- the AI interprets the text
                # regardless of whether it represents a value or label.
                await page.select_option(selector=selector, value=value, prompt=intent, ai=ai_mode, timeout=timeout)  # type: ignore[arg-type]
            else:
                assert selector is not None
                if by_label:
                    # Bypass SkyvernPage to avoid value="" coercion conflicting with label kwarg.
                    await page.page.locator(selector).select_option(label=value, timeout=timeout)
                else:
                    await page.select_option(selector, value=value, timeout=timeout)
            timer.mark("sdk")
        except Exception as e:
            code = ErrorCode.AI_FALLBACK_FAILED if ai_mode else ErrorCode.ACTION_FAILED
            return make_result(
                "skyvern_select_option",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(code, str(e), "Check selector and available options"),
            )

    # NOTE: The SDK select_option() returns the selected value, not a resolved
    # selector. Unlike click(), we cannot return resolved_selector here.
    # SKY-7905 will update the SDK to return element metadata from all action methods.
    data: dict[str, Any] = {"selector": selector, "intent": intent, "ai_mode": ai_mode, "value": value}
    # Build sdk_equivalent: prefer hybrid selector+prompt for production scripts
    if selector and intent:
        data["sdk_equivalent"] = f'await page.select_option("{selector}", value="{value}", prompt="{intent}")'
    elif ai_mode:
        data["sdk_equivalent"] = f'await page.select_option(prompt="{intent}", value="{value}")'
    elif selector:
        data["sdk_equivalent"] = f'await page.select_option("{selector}", value="{value}")'
    return make_result(
        "skyvern_select_option",
        browser_context=ctx,
        data=data,
        timing_ms=timer.timing_ms,
    )


async def skyvern_press_key(
    key: Annotated[str, "Key to press (e.g., Enter, Tab, Escape, ArrowDown)"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    intent: Annotated[
        str | None, Field(description="Natural language description of element to focus first (uses AI)")
    ] = None,
    selector: Annotated[str | None, Field(description="CSS selector to focus first")] = None,
) -> dict[str, Any]:
    """Press a keyboard key -- Enter, Tab, Escape, arrow keys, shortcuts, etc.

    Use `intent` or `selector` to focus a specific element before pressing.
    Without either, presses the key on the currently focused element.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_press_key", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            if intent or selector:
                ai_mode, _ = _resolve_ai_mode(selector, intent)
                if ai_mode is not None:
                    loc = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
                else:
                    assert selector is not None
                    loc = page.locator(selector)
                await loc.press(key)
            else:
                await page.keyboard.press(key)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_press_key",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check key name is valid"),
            )

    if selector and intent:
        sdk_eq = f'await page.locator("{selector}", prompt="{intent}").press("{key}")'
    elif intent:
        sdk_eq = f'await page.locator(prompt="{intent}").press("{key}")'
    elif selector:
        sdk_eq = f'await page.locator("{selector}").press("{key}")'
    else:
        sdk_eq = f'await page.keyboard.press("{key}")'

    return make_result(
        "skyvern_press_key",
        browser_context=ctx,
        data={
            "key": key,
            "selector": selector,
            "intent": intent,
            "sdk_equivalent": sdk_eq,
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_wait(
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    time_ms: Annotated[int | None, Field(description="Time to wait in milliseconds")] = None,
    intent: Annotated[str | None, Field(description="Natural language condition to wait for (uses AI polling)")] = None,
    selector: Annotated[str | None, Field(description="CSS selector to wait for")] = None,
    state: Annotated[str | None, Field(description="Element state: visible, hidden, attached, detached")] = "visible",
    timeout: Annotated[int, Field(description="Max wait time in milliseconds", ge=1000, le=120000)] = 30000,
    poll_interval_ms: Annotated[
        int, Field(description="Polling interval for intent-based waits in ms", ge=500, le=10000)
    ] = 5000,
) -> dict[str, Any]:
    """Wait for a condition, element, or time delay before proceeding. Use intent for AI-powered condition checking.

    Use `intent` to poll with AI validation (e.g., "wait until the loading spinner disappears").
    Use `selector` to wait for an element state. Use `time_ms` for a simple delay.
    """
    valid_states = ("visible", "hidden", "attached", "detached")
    if state is not None and state not in valid_states:
        return make_result(
            "skyvern_wait",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid state: {state}",
                "Use visible, hidden, attached, or detached",
            ),
        )

    if time_ms is None and not selector and not intent:
        return make_result(
            "skyvern_wait",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Must provide intent, selector, or time_ms",
                "Use intent='condition to wait for' for AI-powered waiting, selector='#element' for element visibility, or time_ms=5000 for a delay",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_wait", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            if time_ms is not None:
                await page.wait_for_timeout(time_ms)
                waited_for = "time"
            elif intent:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout / 1000
                last_error: Exception | None = None
                while True:
                    try:
                        result = await page.validate(intent)
                        last_error = None
                    except Exception as poll_err:
                        result = False
                        last_error = poll_err
                    if result:
                        break
                    if loop.time() >= deadline:
                        code = ErrorCode.SDK_ERROR if last_error else ErrorCode.TIMEOUT
                        msg = str(last_error) if last_error else f"Condition not met within {timeout}ms: {intent}"
                        return make_result(
                            "skyvern_wait",
                            ok=False,
                            browser_context=ctx,
                            timing_ms=timer.timing_ms,
                            error=make_error(
                                code,
                                msg,
                                "Increase timeout or check that the condition can be satisfied",
                            ),
                        )
                    await page.wait_for_timeout(poll_interval_ms)
                waited_for = "intent"
            elif selector:
                await page.wait_for_selector(selector, state=state, timeout=timeout)
                waited_for = "selector"
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_wait",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.TIMEOUT, str(e), "Condition was not met within timeout"),
            )

    sdk_eq = ""
    if waited_for == "time":
        sdk_eq = f"await page.wait_for_timeout({time_ms})"
    elif waited_for == "intent":
        sdk_eq = f'await page.validate("{intent}")'
    elif waited_for == "selector":
        sdk_eq = f'await page.wait_for_selector("{selector}")'
    return make_result(
        "skyvern_wait",
        browser_context=ctx,
        data={"waited_for": waited_for, "sdk_equivalent": sdk_eq},
        timing_ms=timer.timing_ms,
    )


async def skyvern_evaluate(
    expression: Annotated[str, "JavaScript expression to evaluate"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Run JavaScript on the page to read DOM state, get URLs, check values, or discover CSS selectors for faster subsequent actions.

    Security: This executes arbitrary JS in the page context. Only use with trusted expressions.
    """
    # Block JS that sets password field values
    if JS_PASSWORD_PATTERN.search(expression):
        return make_result(
            "skyvern_evaluate",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cannot set password field values via JavaScript — credentials must not be passed through tool calls",
                CREDENTIAL_HINT,
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_evaluate", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await page.evaluate(expression)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_evaluate",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.ACTION_FAILED, str(e), "Check JavaScript syntax"),
            )

    return make_result(
        "skyvern_evaluate",
        browser_context=ctx,
        data={"result": result, "sdk_equivalent": f'await page.evaluate("{expression[:80]}")'},
        timing_ms=timer.timing_ms,
    )


async def skyvern_extract(
    prompt: Annotated[str, "Natural language description of what data to extract from the page"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    schema: Annotated[
        str | None, Field(description="JSON Schema string defining the expected output structure")
    ] = None,
) -> dict[str, Any]:
    """Get structured data from any website — prices, listings, articles, tables, contact info, etc. Use this instead of writing scraping code, curl commands, or guessing API endpoints. Describe what you need in natural language and get JSON back.

    Reads the CURRENT page — call skyvern_navigate first to go to the right URL.
    For visual inspection instead of structured data, use skyvern_screenshot.
    Optionally provide a JSON `schema` to enforce the output structure (pass as a JSON string).
    """
    if schema is not None:
        try:
            parsed_schema = parse_extract_schema(schema)
        except GuardError as e:
            return make_result(
                "skyvern_extract",
                ok=False,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
            )
    else:
        parsed_schema = None

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_extract", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await do_extract(page, prompt, schema=parsed_schema)
            timer.mark("sdk")
        except GuardError as e:
            return make_result(
                "skyvern_extract",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
            )
        except Exception as e:
            return make_result(
                "skyvern_extract",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Check that the page has loaded and the prompt is clear"),
            )

    return make_result(
        "skyvern_extract",
        browser_context=ctx,
        data={
            "extracted": result.extracted,
            "sdk_equivalent": f'await page.extract(prompt="{prompt}")',
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_validate(
    prompt: Annotated[str, "Validation condition to check (e.g., 'the login form is visible')"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Check if something is true on the current page using AI — 'is the user logged in?', 'does the cart have 3 items?', 'is the form submitted?'

    Reads the CURRENT page — navigate first. Returns true/false.
    To extract data (not just check a condition), use skyvern_extract instead.
    """
    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_validate", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            valid = await page.validate(prompt)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_validate",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Check that the page has loaded and the prompt is clear"),
            )

    return make_result(
        "skyvern_validate",
        browser_context=ctx,
        data={"prompt": prompt, "valid": valid, "sdk_equivalent": f'await page.validate("{prompt}")'},
        timing_ms=timer.timing_ms,
    )


async def skyvern_act(
    prompt: Annotated[str, "Natural language instruction for the action to perform (e.g., 'close the cookie banner')"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
) -> dict[str, Any]:
    """Perform actions on a web page by describing what to do in plain English — click buttons, close popups, fill forms, scroll to sections, interact with menus. Replaces multi-step snapshot→click→snapshot→click sequences with a single natural language instruction.

    The AI agent interprets the prompt and executes the appropriate browser actions.
    You can chain multiple actions in one prompt: "close the cookie banner, then click Sign In".
    NEVER include passwords or credentials in the prompt. Use skyvern_login with a stored credential instead. Create credentials via CLI: skyvern credentials add.
    For multi-step automations (4+ pages), use skyvern_workflow_create with one block per step.
    For quick one-off multi-page tasks, use skyvern_run_task.
    """
    try:
        check_password_prompt(prompt)
    except GuardError as e:
        return make_result(
            "skyvern_act",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_act", ok=False, error=no_browser_error())

    with Timer() as timer:
        try:
            result = await do_act(page, prompt)
            timer.mark("sdk")
        except GuardError as e:
            return make_result(
                "skyvern_act",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint),
            )
        except Exception as e:
            return make_result(
                "skyvern_act",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Simplify the prompt or break the task into steps"),
            )

    return make_result(
        "skyvern_act",
        browser_context=ctx,
        data={
            "prompt": result.prompt,
            "completed": result.completed,
            "sdk_equivalent": f'await page.act("{prompt}")',
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_run_task(
    prompt: Annotated[str, "Natural language description of the task to automate"],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    url: Annotated[
        str | None, Field(description="URL to navigate to before running (uses current page if omitted)")
    ] = None,
    data_extraction_schema: Annotated[
        str | None, Field(description="JSON Schema string defining what data to extract")
    ] = None,
    max_steps: Annotated[int | None, Field(description="Maximum number of agent steps")] = None,
    timeout_seconds: Annotated[
        int, Field(description="Timeout in seconds (default 180s = 3 minutes)", ge=10, le=1800)
    ] = 180,
) -> dict[str, Any]:
    """Run a quick, one-off web task via an autonomous AI agent. Nothing is saved — use for throwaway tests and exploration only. Best for tasks describable in 2-3 sentences.

    Always uses engine 2.0 (planning agent) — the engine cannot be changed. For simple single-goal
    tasks, a workflow with engine 1.0 blocks is cheaper and more reliable.

    For anything reusable, multi-step, or worth keeping, use skyvern_workflow_create instead — it produces a versioned, rerunnable workflow with per-step observability.
    For simple single-step actions on the current page, use skyvern_act instead.
    """
    # Block password/credential actions — redirect to skyvern_login
    if PASSWORD_PATTERN.search(prompt):
        return make_result(
            "skyvern_run_task",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Cannot perform password/credential actions — credentials must not be passed through tool calls",
                CREDENTIAL_HINT,
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_run_task", ok=False, error=no_browser_error())

    parsed_schema: dict[str, Any] | str | None = None
    if data_extraction_schema is not None:
        try:
            parsed_schema = json.loads(data_extraction_schema)
        except (json.JSONDecodeError, TypeError) as e:
            return make_result(
                "skyvern_run_task",
                ok=False,
                browser_context=ctx,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid data_extraction_schema JSON: {e}",
                    "Provide schema as a valid JSON string",
                ),
            )

    with Timer() as timer:
        try:
            response = await page.agent.run_task(
                prompt=prompt,
                url=url,
                data_extraction_schema=parsed_schema,
                max_steps=max_steps,
                timeout=timeout_seconds,
            )
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_run_task",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Check the prompt, URL, and timeout settings"),
            )

    return make_result(
        "skyvern_run_task",
        browser_context=ctx,
        data={
            "run_id": response.run_id,
            "status": response.status,
            "output": response.output,
            "failure_reason": response.failure_reason,
            "recording_url": response.recording_url,
            "app_url": response.app_url,
            "sdk_equivalent": f'await page.agent.run_task(prompt="{prompt}")',
        },
        timing_ms=timer.timing_ms,
    )


# Maps credential_type string → required fields for validation
_CREDENTIAL_REQUIRED_FIELDS: dict[CredentialType, list[str]] = {
    CredentialType.skyvern: ["credential_id"],
    CredentialType.bitwarden: ["bitwarden_item_id"],
    CredentialType.onepassword: ["onepassword_vault_id", "onepassword_item_id"],
    CredentialType.azure_vault: ["azure_vault_name", "azure_vault_username_key", "azure_vault_password_key"],
}


async def skyvern_login(
    credential_type: Annotated[
        str, Field(description="Credential provider: 'skyvern', 'bitwarden', '1password', or 'azure_vault'")
    ] = "skyvern",
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    url: Annotated[str | None, Field(description="Login page URL. Uses current page if omitted")] = None,
    credential_id: Annotated[str | None, Field(description="Skyvern credential ID (for type='skyvern')")] = None,
    bitwarden_item_id: Annotated[str | None, Field(description="Bitwarden item ID (for type='bitwarden')")] = None,
    bitwarden_collection_id: Annotated[str | None, Field(description="Bitwarden collection ID (optional)")] = None,
    onepassword_vault_id: Annotated[str | None, Field(description="1Password vault ID (for type='1password')")] = None,
    onepassword_item_id: Annotated[str | None, Field(description="1Password item ID (for type='1password')")] = None,
    azure_vault_name: Annotated[str | None, Field(description="Azure Vault name (for type='azure_vault')")] = None,
    azure_vault_username_key: Annotated[str | None, Field(description="Azure Vault username key")] = None,
    azure_vault_password_key: Annotated[str | None, Field(description="Azure Vault password key")] = None,
    azure_vault_totp_secret_key: Annotated[str | None, Field(description="Azure Vault TOTP key (optional)")] = None,
    prompt: Annotated[str | None, Field(description="Additional login instructions")] = None,
    totp_identifier: Annotated[str | None, Field(description="TOTP identifier for 2FA")] = None,
    totp_url: Annotated[str | None, Field(description="URL to fetch TOTP codes")] = None,
    timeout_seconds: Annotated[int, Field(description="Timeout in seconds (default 180)", ge=10, le=600)] = 180,
) -> dict[str, Any]:
    """Log into a website using stored credentials from Skyvern, Bitwarden, 1Password, or Azure Vault. Passwords are never exposed in prompts.

    Requires a browser session. The AI agent handles the full login flow — finding fields, entering credentials, handling 2FA — so you don't need to write selectors.
    After login, use skyvern_screenshot to verify success, then continue with other browser tools.
    """
    # Validate credential_type
    try:
        cred_type = CredentialType(credential_type)
    except ValueError:
        valid = ", ".join(f"'{v.value}'" for v in CredentialType)
        return make_result(
            "skyvern_login",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid credential_type: '{credential_type}'",
                f"Use one of: {valid}",
            ),
        )

    # Validate required fields per credential type
    local_vars = {
        "credential_id": credential_id,
        "bitwarden_item_id": bitwarden_item_id,
        "onepassword_vault_id": onepassword_vault_id,
        "onepassword_item_id": onepassword_item_id,
        "azure_vault_name": azure_vault_name,
        "azure_vault_username_key": azure_vault_username_key,
        "azure_vault_password_key": azure_vault_password_key,
    }
    missing = [f for f in _CREDENTIAL_REQUIRED_FIELDS[cred_type] if not local_vars.get(f)]
    if missing:
        return make_result(
            "skyvern_login",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Missing required fields for credential_type='{cred_type.value}': {', '.join(missing)}",
                f"Provide: {', '.join(missing)}",
            ),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError:
        return make_result("skyvern_login", ok=False, error=no_browser_error())

    # Common kwargs shared across all credential types
    _common_kwargs: dict[str, Any] = {"url": url, "prompt": prompt, "timeout": timeout_seconds}
    if totp_identifier is not None:
        _common_kwargs["totp_identifier"] = totp_identifier
    if totp_url is not None:
        _common_kwargs["totp_url"] = totp_url

    with Timer() as timer:
        try:
            # Dispatch per credential type to satisfy mypy's overloaded signatures
            if cred_type == CredentialType.skyvern:
                assert credential_id is not None
                response = await page.agent.login(
                    credential_type=CredentialType.skyvern,
                    credential_id=credential_id,
                    **_common_kwargs,
                )
            elif cred_type == CredentialType.bitwarden:
                assert bitwarden_item_id is not None
                response = await page.agent.login(
                    credential_type=CredentialType.bitwarden,
                    bitwarden_item_id=bitwarden_item_id,
                    bitwarden_collection_id=bitwarden_collection_id,
                    **_common_kwargs,
                )
            elif cred_type == CredentialType.onepassword:
                assert onepassword_vault_id is not None and onepassword_item_id is not None
                response = await page.agent.login(
                    credential_type=CredentialType.onepassword,
                    onepassword_vault_id=onepassword_vault_id,
                    onepassword_item_id=onepassword_item_id,
                    **_common_kwargs,
                )
            else:
                assert azure_vault_name is not None
                assert azure_vault_username_key is not None
                assert azure_vault_password_key is not None
                response = await page.agent.login(
                    credential_type=CredentialType.azure_vault,
                    azure_vault_name=azure_vault_name,
                    azure_vault_username_key=azure_vault_username_key,
                    azure_vault_password_key=azure_vault_password_key,
                    azure_vault_totp_secret_key=azure_vault_totp_secret_key,
                    **_common_kwargs,
                )
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_login",
                ok=False,
                browser_context=ctx,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.SDK_ERROR,
                    str(e),
                    "Check credential_type and required fields for your credential provider",
                ),
            )

    return make_result(
        "skyvern_login",
        browser_context=ctx,
        data={
            "run_id": response.run_id,
            "status": response.status,
            "output": response.output,
            "failure_reason": response.failure_reason,
            "recording_url": response.recording_url,
            "app_url": response.app_url,
            "sdk_equivalent": f"await page.agent.login(credential_type=CredentialType.{cred_type.name})",
        },
        timing_ms=timer.timing_ms,
    )
