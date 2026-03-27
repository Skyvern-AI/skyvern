from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog

from skyvern.constants import PERMANENT_NAV_ERRORS, SKIP_INNER_NAV_RETRY_ERRORS
from skyvern.exceptions import FailedToNavigateToUrl

LOG = structlog.get_logger()

NavigateFunc = Callable[[], Awaitable[object]]
SettleFunc = Callable[[], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]


def is_skip_inner_retry_error(error_message: str) -> bool:
    return any(pattern in error_message for pattern in SKIP_INNER_NAV_RETRY_ERRORS)


def is_permanent_navigation_error(error_message: str) -> bool:
    return any(pattern in error_message for pattern in PERMANENT_NAV_ERRORS)


async def navigate_with_retry(
    navigate: NavigateFunc,
    url: str,
    retry_times: int,
    settle: SettleFunc,
    sleep: SleepFunc = asyncio.sleep,
) -> None:
    for attempt in range(retry_times):
        LOG.info("Trying to navigate to url", url=url, retry_time=attempt)
        try:
            start_time = time.time()
            await navigate()
            elapsed = time.time() - start_time
            LOG.info("Page loading time", loading_time=elapsed, url=url)
            await settle()
            LOG.info("Successfully navigated to url", url=url, retry_time=attempt)
            return

        except Exception as error:
            error_str = str(error)

            if is_skip_inner_retry_error(error_str):
                LOG.warning(
                    "Non-retriable navigation error, failing immediately",
                    url=url,
                    error=error_str,
                )
                raise FailedToNavigateToUrl(url=url, error_message=error_str)

            if attempt >= retry_times - 1:
                LOG.exception(
                    "Failed to navigate after retries",
                    url=url,
                    retry_times=retry_times,
                    error=error_str,
                )
                raise FailedToNavigateToUrl(url=url, error_message=error_str)

            LOG.warning(
                "Error while navigating to url, retrying",
                exc_info=True,
                url=url,
                retry_time=attempt,
                error=error_str,
            )
            await sleep(1)
