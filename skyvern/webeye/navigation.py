from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog

from skyvern.constants import PERMANENT_NAV_ERRORS, SKIP_INNER_NAV_RETRY_ERRORS
from skyvern.exceptions import FailedToNavigateToUrl

LOG = structlog.get_logger()

NavigateFunc = Callable[[str], Awaitable[object]]
SettleFunc = Callable[[], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]

# Progressive wait_until degradation. Degrading to `domcontentloaded` and
# then `commit` lets navigation succeed once the DOM or response is ready.
_DEGRADATION_MAP: dict[str, list[str]] = {
    "load": ["load", "domcontentloaded", "commit"],
    "domcontentloaded": ["domcontentloaded", "commit"],
    "commit": ["commit"],
}


def is_skip_inner_retry_error(error_message: str) -> bool:
    return any(pattern in error_message for pattern in SKIP_INNER_NAV_RETRY_ERRORS)


def is_permanent_navigation_error(error_message: str) -> bool:
    return any(pattern in error_message for pattern in PERMANENT_NAV_ERRORS)


async def navigate_with_retry(
    navigate: NavigateFunc,
    url: str,
    retry_times: int,
    settle: SettleFunc,
    wait_until: Literal["load", "domcontentloaded", "commit"] = "load",
    sleep: SleepFunc = asyncio.sleep,
) -> None:
    degradation = _DEGRADATION_MAP.get(wait_until, [wait_until])

    for attempt in range(retry_times):
        strategy = degradation[min(attempt, len(degradation) - 1)]
        LOG.info("Trying to navigate to url", url=url, retry_time=attempt, wait_until=strategy)
        try:
            start_time = time.monotonic()
            await navigate(strategy)
            elapsed = time.monotonic() - start_time
            LOG.info("Page loading time", loading_time=elapsed, url=url, wait_until=strategy)
            await settle()
            LOG.info("Successfully navigated to url", url=url, retry_time=attempt, wait_until=strategy)
            return

        except Exception as error:
            error_str = str(error)

            if is_skip_inner_retry_error(error_str):
                LOG.warning(
                    "Non-retriable navigation error, failing immediately",
                    url=url,
                    error=error_str,
                )
                raise FailedToNavigateToUrl(url=url, error_message=error_str) from error

            if attempt >= retry_times - 1:
                LOG.exception(
                    "Failed to navigate after retries",
                    url=url,
                    retry_times=retry_times,
                    error=error_str,
                )
                raise FailedToNavigateToUrl(url=url, error_message=error_str) from error

            LOG.warning(
                "Error while navigating to url, retrying",
                exc_info=True,
                url=url,
                retry_time=attempt,
                wait_until=strategy,
                error=error_str,
            )
            await sleep(1)
