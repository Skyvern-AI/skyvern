from typing import Any

import structlog
from playwright.async_api import Locator

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

    await locator.press_sequentially(text, delay=TEXT_INPUT_DELAY, timeout=timeout)
