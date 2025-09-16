import asyncio
from typing import Any, Literal

import structlog
from playwright.async_api import Locator, Page

from skyvern.config import settings
from skyvern.constants import TEXT_INPUT_DELAY, TEXT_PRESS_MAX_LENGTH
from skyvern.forge.sdk.api.files import download_file as download_file_api

LOG = structlog.get_logger()


async def download_file(file_url: str, action: dict[str, Any] | None = None) -> str | list[str]:
    try:
        return await download_file_api(file_url)
    except Exception:
        LOG.exception(
            "Failed to download file, continuing without it",
            action=action,
            file_url=file_url,
        )
        return []


async def input_sequentially(locator: Locator, text: str, timeout: float = settings.BROWSER_ACTION_TIMEOUT_MS) -> None:
    length = len(text)
    if length > TEXT_PRESS_MAX_LENGTH:
        # if the text is longer than TEXT_PRESS_MAX_LENGTH characters, we will locator.fill in initial texts until the last TEXT_PRESS_MAX_LENGTH characters
        # and then type the last TEXT_PRESS_MAX_LENGTH characters with locator.press_sequentially
        await locator.fill(text[: length - TEXT_PRESS_MAX_LENGTH], timeout=timeout)
        text = text[length - TEXT_PRESS_MAX_LENGTH :]

    for char in text:
        await locator.type(char, delay=TEXT_INPUT_DELAY, timeout=timeout)


async def keypress(page: Page, keys: list[str], hold: bool = False, duration: float = 0) -> None:
    updated_keys = []
    for key in keys:
        key_lower_case = key.lower()
        if key_lower_case in ("enter", "return"):
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
    if hold:
        await page.keyboard.down(keypress_str)
        await asyncio.sleep(duration)
        await page.keyboard.up(keypress_str)
    else:
        await page.keyboard.press(keypress_str)


async def drag(
    page: Page, start_x: int | None = None, start_y: int | None = None, path: list[tuple[int, int]] | None = None
) -> None:
    if start_x and start_y:
        await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    path = path or []
    for point in path:
        x, y = point[0], point[1]
        await page.mouse.move(x, y)
    await page.mouse.up()


async def left_mouse(page: Page, x: int | None, y: int | None, direction: Literal["down", "up"]) -> None:
    if x and y:
        await page.mouse.move(x, y)
    if direction == "down":
        await page.mouse.down()
    elif direction == "up":
        await page.mouse.up()
    else:
        LOG.info("Invalid direction for left mouse action", direction=direction)
