import asyncio
from typing import Any, Literal

import structlog
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.config import settings
from skyvern.constants import TEXT_PRESS_MAX_LENGTH
from skyvern.forge.sdk.api.files import download_file as download_file_api
from skyvern.forge.sdk.api.files import resolve_run_download_id, validate_local_file_path
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.webeye.actions.actions import Action, KeypressAction

LOG = structlog.get_logger()

_NATIVE_VALUE_SET_INPUT_TYPES = frozenset({"range", "date", "datetime-local", "month", "time", "week"})


async def _uses_native_value_set_fill(locator: Locator) -> bool:
    try:
        input_type = await locator.evaluate("el => el.tagName === 'INPUT' ? el.type : null")
    except Exception:
        LOG.debug("Failed to inspect input type before strategy-aware fill", exc_info=True)
        return False
    return input_type in _NATIVE_VALUE_SET_INPUT_TYPES


async def download_file(
    file_url: str,
    action: dict[str, Any] | None = None,
    organization_id: str | None = None,
) -> str | list[str]:
    if file_url.startswith("/"):
        run_id = resolve_run_download_id(skyvern_context.current())
        try:
            return validate_local_file_path(file_url, run_id)
        except PermissionError:
            # Expected when the LLM/user supplies an absolute path outside the run's download
            # directory. The containment guard is doing its job; the action fails cleanly rather
            # than surfacing as an unhandled ERROR with a traceback.
            LOG.warning(
                "Local file path is outside the allowed download directory, continuing without it",
                action=action,
                file_url=file_url,
                run_id=run_id,
            )
            return []

    try:
        return await download_file_api(file_url, organization_id=organization_id)
    except Exception:
        # Fully self-recovering: the action proceeds without the file.
        LOG.warning(
            "Failed to download file, continuing without it",
            action=action,
            file_url=file_url,
            exc_info=True,
        )
        return []


async def strategy_aware_input(
    locator: Locator,
    text: str,
    *,
    clear: bool | None,
    timeout: float | None = settings.BROWSER_ACTION_TIMEOUT_MS,
    use_caller_timeout: bool = True,
    force: bool | None = None,
    delay: float | None = None,
    no_wait_after: bool | None = None,
    dispatch_change_and_blur: bool = False,
    allow_batched_playwright: bool = True,
) -> None:
    length = len(text)
    prefix = text[: length - TEXT_PRESS_MAX_LENGTH] if length > TEXT_PRESS_MAX_LENGTH else None
    strategy_text = text[length - TEXT_PRESS_MAX_LENGTH :] if prefix is not None and clear is not False else text

    async def dispatch_commit_events() -> None:
        if not dispatch_change_and_blur:
            return
        # Playwright typing already emits input events. Preserve the deterministic replace
        # contract for JS-gated forms by adding the commit events that typing does not emit.
        for event_name in ("change", "blur"):
            try:
                await locator.dispatch_event(event_name, timeout=timeout)
            except Exception:
                # These events were historically best-effort; a dispatch failure must not
                # turn a successful text replacement into a failed action.
                LOG.debug("Failed to dispatch post-input event", event_name=event_name, exc_info=True)

    async def perform_input() -> None:
        if clear is True and await _uses_native_value_set_fill(locator):
            native_fill_options: dict[str, float | bool | None] = {"timeout": timeout}
            if force is not None:
                native_fill_options["force"] = force
            if no_wait_after is not None:
                native_fill_options["no_wait_after"] = no_wait_after
            await locator.fill(text, **native_fill_options)
            await dispatch_commit_events()
            return

        if clear is True and force is True:
            force_fill_options: dict[str, float | bool | None] = {"timeout": timeout, "force": True}
            if no_wait_after is not None:
                force_fill_options["no_wait_after"] = no_wait_after
            await locator.fill(text, **force_fill_options)
            await dispatch_commit_events()
            return

        # None preserves input_sequentially's legacy contract: short values append, while long values seed a prefix.
        if clear is True and prefix is None:
            if use_caller_timeout:
                if force is None and no_wait_after is None:
                    await EventStrategyFactory.clear_field(locator.page, locator, char_count=0, timeout=timeout)
                else:
                    await EventStrategyFactory.clear_field(
                        locator.page,
                        locator,
                        char_count=0,
                        timeout=timeout,
                        force=force,
                        no_wait_after=no_wait_after,
                    )
            else:
                await EventStrategyFactory.clear_field(locator.page, locator, char_count=0)

        if prefix is not None and clear is not False:
            # Keep large replacements fast, then send the tail through the active strategy.
            fill_options: dict[str, float | bool | None] = {"timeout": timeout}
            if force is not None:
                fill_options["force"] = force
            if no_wait_after is not None:
                fill_options["no_wait_after"] = no_wait_after
            await locator.fill(prefix, **fill_options)

        if use_caller_timeout:
            if delay is None and no_wait_after is None:
                await EventStrategyFactory.type_text(
                    locator.page,
                    locator,
                    strategy_text,
                    timeout=timeout,
                    allow_batched_playwright=allow_batched_playwright,
                )
            else:
                await EventStrategyFactory.type_text(
                    locator.page,
                    locator,
                    strategy_text,
                    timeout=timeout,
                    delay=delay,
                    no_wait_after=no_wait_after,
                    allow_batched_playwright=allow_batched_playwright,
                )
        else:
            # Keep input_sequentially's pre-existing strategy timeout behavior byte-for-byte.
            await EventStrategyFactory.type_text(locator.page, locator, strategy_text)

        await dispatch_commit_events()

    if not use_caller_timeout:
        await perform_input()
        return

    # One authored Playwright operation receives one total deadline. Playwright defines zero as
    # disabling timeout enforcement, which asyncio.timeout(None) preserves.
    timeout_seconds = None if timeout is None or timeout == 0 else timeout / 1000
    try:
        async with asyncio.timeout(timeout_seconds):
            await perform_input()
    except TimeoutError as exc:
        # Keep the intercepted operation indistinguishable from authored Playwright fill/type calls
        # to callers such as skyvern_type and code blocks that catch Playwright's TimeoutError.
        raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded.") from exc


async def input_sequentially(locator: Locator, text: str, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
    await strategy_aware_input(
        locator,
        text,
        clear=None,
        timeout=timeout,
        use_caller_timeout=False,
        allow_batched_playwright=False,
    )


ENTER_KEY_ALIASES = ("enter", "return")


def keys_include_enter(keys: list[str]) -> bool:
    # "enter" and "return" both execute as the Enter key (see keypress() below).
    return any(key.lower() in ENTER_KEY_ALIASES for key in keys)


def should_stop_batch_after_dropdown_select(next_action: Action | None) -> bool:
    """Whether a committed in-INPUT_TEXT combobox selection should stop the batch.

    True only when the next batched action would clobber the selection: a trailing Enter/Return
    keypress. Same-element / other follow-ups are intentionally left to run.
    """
    return isinstance(next_action, KeypressAction) and keys_include_enter(next_action.keys)


async def keypress(page: Page, keys: list[str], hold: bool = False, duration: float = 0, repeat: int = 1) -> None:
    updated_keys = []
    for key in keys:
        key_lower_case = key.lower()
        if key_lower_case in ENTER_KEY_ALIASES:
            updated_keys.append("Enter")
        elif key_lower_case == "space":
            updated_keys.append(" ")
        elif key_lower_case == "ctrl":
            updated_keys.append("Control")
        elif key_lower_case == "backspace":
            updated_keys.append("Backspace")
        elif key_lower_case == "pagedown":
            updated_keys.append("PageDown")
        elif key_lower_case == "pageup":
            updated_keys.append("PageUp")
        elif key_lower_case == "tab":
            updated_keys.append("Tab")
        elif key_lower_case == "shift":
            updated_keys.append("Shift")
        elif key_lower_case in ("arrowleft", "left"):
            updated_keys.append("ArrowLeft")
        elif key_lower_case in ("arrowright", "right"):
            updated_keys.append("ArrowRight")
        elif key_lower_case in ("arrowup", "up"):
            updated_keys.append("ArrowUp")
        elif key_lower_case in ("arrowdown", "down"):
            updated_keys.append("ArrowDown")
        elif key_lower_case == "home":
            updated_keys.append("Home")
        elif key_lower_case == "end":
            updated_keys.append("End")
        elif key_lower_case == "delete":
            updated_keys.append("Delete")
        elif key_lower_case == "esc":
            updated_keys.append("Escape")
        elif key_lower_case == "alt":
            updated_keys.append("Alt")
        elif key_lower_case.startswith("f") and key_lower_case[1:].isdigit():
            # Handle function keys: f1 -> F1, f5 -> F5, etc.
            updated_keys.append(key_lower_case.upper())
        else:
            updated_keys.append(key)
    keypress_str = "+".join(updated_keys)
    n = max(1, repeat)
    if hold:
        await page.keyboard.down(keypress_str)
        await asyncio.sleep(duration)
        await page.keyboard.up(keypress_str)
    else:
        for _ in range(n):
            await page.keyboard.press(keypress_str)


async def drag(
    page: Page, start_x: int | None = None, start_y: int | None = None, path: list[tuple[int, int]] | None = None
) -> None:
    if start_x and start_y:
        await EventStrategyFactory.move_cursor(page, start_x, start_y)
    await page.mouse.down()
    path = path or []
    last_x: float = start_x if start_x is not None else 0.0
    last_y: float = start_y if start_y is not None else 0.0
    for point in path:
        last_x, last_y = point[0], point[1]
        await EventStrategyFactory.move_cursor(page, last_x, last_y)
    await page.mouse.up()
    # Sync cursor strategy position to the final drag point so the next
    # move_to() starts from the correct location.
    if start_x is not None or path:
        EventStrategyFactory.sync_cursor_position(page, last_x, last_y)


async def left_mouse(page: Page, x: int | None, y: int | None, direction: Literal["down", "up"]) -> None:
    if x and y:
        await EventStrategyFactory.move_cursor(page, x, y)
    if direction == "down":
        await page.mouse.down()
    elif direction == "up":
        await page.mouse.up()
    else:
        LOG.info("Invalid direction for left mouse action", direction=direction)
