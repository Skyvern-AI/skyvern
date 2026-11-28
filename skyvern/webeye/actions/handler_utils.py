import asyncio
from collections import Counter
from contextlib import suppress
from typing import Any, Literal

import structlog
from playwright.async_api import ElementHandle, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.config import settings
from skyvern.constants import TEXT_PRESS_MAX_LENGTH
from skyvern.forge.sdk.api.files import download_file as download_file_api
from skyvern.forge.sdk.api.files import resolve_run_download_id, validate_local_file_path
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.webeye.actions.actions import Action, KeypressAction
from skyvern.webeye.actions.key_names import ENTER_KEY_ALIASES, normalize_key_name

LOG = structlog.get_logger()

_NATIVE_VALUE_SET_INPUT_TYPES = frozenset({"range", "date", "datetime-local", "month", "time", "week"})
_READBACK_TIMEOUT_MS = 500
# Playwright's fill waits for visibility that checkVisibility() misses (visibility:hidden, zero size); cap that wait.
_CORRECTION_TIMEOUT_MS = 1000
_READBACK_STATE_JS = (
    "el => ({value: typeof el.value === 'string' ? el.value : null,"
    " editable: el.isConnected && !el.matches(':disabled') && !el.readOnly"
    " && (!el.checkVisibility || el.checkVisibility())})"
)


class UploadFileDownloadError(Exception):
    pass


async def _uses_native_value_set_fill(locator: Locator) -> bool:
    try:
        input_type = await locator.evaluate("el => el.tagName === 'INPUT' ? el.type : null")
    except Exception:
        LOG.debug("Failed to inspect input type before strategy-aware fill", exc_info=True)
        return False
    return input_type in _NATIVE_VALUE_SET_INPUT_TYPES


async def _typed_element_handle(locator: Locator) -> ElementHandle | None:
    try:
        return await locator.element_handle(timeout=_READBACK_TIMEOUT_MS)
    except Exception:
        LOG.debug("Failed to resolve the typed element for read-back", exc_info=True)
        return None


async def _rebind_if_replaced(handle: ElementHandle, locator: Locator) -> ElementHandle | None:
    try:
        connected = await asyncio.wait_for(handle.evaluate("el => el.isConnected"), _READBACK_TIMEOUT_MS / 1000)
    except Exception:
        # The evaluate fails when a navigation destroyed the context: keep the stale handle so the read-back declines.
        LOG.debug("Typed element context is gone after the clear or prefix fill", exc_info=True)
        return handle
    if connected is not False:
        return handle
    # Same document, new node (a field re-rendered on focus or first input): follow it.
    with suppress(Exception):
        await handle.dispose()
    return await _typed_element_handle(locator)


async def _input_value_scrambled(handle: ElementHandle, text: str) -> bool:
    try:
        # Short bound: the field may be gone after the last key (auto-submit), and a completed input must not fail.
        state = await asyncio.wait_for(handle.evaluate(_READBACK_STATE_JS), _READBACK_TIMEOUT_MS / 1000)
    except Exception:
        LOG.debug("Failed to read field value after strategy-aware input", exc_info=True)
        return False
    # Detached, hidden, disabled or read-only fields (auto-submitted code boxes) and elements without a form value
    # (contenteditable) keep the typed result.
    value = state.get("value") if isinstance(state, dict) else None
    if not isinstance(value, str) or state.get("editable") is not True:
        return False
    # Pages that filter, truncate or clear the typed keys leave a subsequence of the text, and refilling them can
    # resubmit codes. A focus steal reorders keys (plus a stray typo letter its Backspace missed), so it is not one.
    if _is_subsequence(value, text) or _is_subsequence(text, value):
        return False
    return Counter(value) <= Counter(text) or Counter(text) <= Counter(value)


def _is_subsequence(short: str, long: str) -> bool:
    remaining = iter(long)
    return all(char in remaining for char in short)


async def download_file(
    file_url: str,
    action: dict[str, Any] | None = None,
    organization_id: str | None = None,
) -> str:
    if file_url.startswith("/"):
        run_id = resolve_run_download_id(skyvern_context.current())
        try:
            return validate_local_file_path(file_url, run_id)
        except PermissionError as exc:
            # Expected when the LLM/user supplies an absolute path outside the run's download
            # directory. The containment guard is doing its job; the caller turns this into an
            # action failure.
            LOG.warning(
                "Local file path is outside the allowed download directory",
                action=action,
                file_url=file_url,
                run_id=run_id,
            )
            raise UploadFileDownloadError("Local file path is outside the allowed download directory") from exc

    try:
        return await download_file_api(file_url, organization_id=organization_id)
    except Exception as exc:
        LOG.warning(
            "Failed to download file",
            action=action,
            file_url=file_url,
            exc_info=True,
        )
        raise UploadFileDownloadError("File download failed") from exc


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
    fill_options: dict[str, float | bool | None] = {"timeout": timeout}
    if force is not None:
        fill_options["force"] = force
    if no_wait_after is not None:
        fill_options["no_wait_after"] = no_wait_after

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

    async def perform_input() -> ElementHandle | None:
        if clear is True and (force is True or await _uses_native_value_set_fill(locator)):
            await locator.fill(text, **fill_options)
            await dispatch_commit_events()
            return None

        # Bind the read-back and any correction to this element before any step can navigate, so a page that leaves
        # mid-input cannot redirect the full value into a same-selector field elsewhere.
        handle = await _typed_element_handle(locator) if clear is True else None

        try:
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
                await locator.fill(prefix, **fill_options)

            if handle is not None:
                handle = await _rebind_if_replaced(handle, locator)

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
        except BaseException:
            if handle is not None:
                with suppress(Exception):
                    await handle.dispose()
            raise
        return handle

    # One authored Playwright operation receives one total deadline. Playwright defines zero as
    # disabling timeout enforcement, which asyncio.timeout(None) preserves.
    timeout_seconds = None if timeout is None or timeout == 0 else timeout / 1000
    loop = asyncio.get_running_loop()
    started = loop.time()
    if not use_caller_timeout:
        handle = await perform_input()
    else:
        try:
            async with asyncio.timeout(timeout_seconds):
                handle = await perform_input()
        except TimeoutError as exc:
            # Keep the intercepted operation indistinguishable from authored Playwright fill/type calls
            # to callers such as skyvern_type and code blocks that catch Playwright's TimeoutError.
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded.") from exc
    if handle is None:
        return

    try:
        # The read-back and correction are best-effort: the typed input was already delivered, so neither may fail it.
        if await _input_value_scrambled(handle, text):
            # Playwright re-focuses each typed key with the caret at 0 when the page stole focus mid-typing,
            # so the tail lands reversed at the front. A replace should still end with exactly `text`.
            fill_timeout_ms: float = _CORRECTION_TIMEOUT_MS
            if timeout_seconds is not None:
                fill_timeout_ms = min(fill_timeout_ms, (timeout_seconds - (loop.time() - started)) * 1000)
            if fill_timeout_ms >= 1:
                LOG.info("Typed value did not land intact, falling back to a single fill")
                await handle.fill(text, **{**fill_options, "timeout": fill_timeout_ms})
                if dispatch_change_and_blur:
                    for event_name in ("change", "blur"):
                        with suppress(Exception):
                            await handle.dispatch_event(event_name)
    except Exception as exc:  # noqa: BLE001 - Playwright's error text logs the filled value; keep only its type.
        LOG.debug("Corrective fill failed, keeping the typed value", error_type=type(exc).__name__)
    finally:
        with suppress(Exception):
            await handle.dispose()


async def input_sequentially(locator: Locator, text: str, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
    await strategy_aware_input(
        locator,
        text,
        clear=None,
        timeout=timeout,
        use_caller_timeout=False,
        allow_batched_playwright=False,
    )


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
    updated_keys = [normalize_key_name(key) for key in keys]
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
    # A coordinate of 0 is the viewport's left/top edge, not "no start point": only None means unset,
    # as it already does for last_x/last_y below.
    if start_x is not None and start_y is not None:
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
    # Same convention as drag(): 0 is an edge coordinate and only None means "no position given".
    if x is not None and y is not None:
        await EventStrategyFactory.move_cursor(page, x, y)
    if direction == "down":
        await page.mouse.down()
    elif direction == "up":
        await page.mouse.up()
    else:
        LOG.info("Invalid direction for left mouse action", direction=direction)
