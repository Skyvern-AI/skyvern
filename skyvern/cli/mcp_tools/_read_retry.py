from __future__ import annotations

from asyncio import sleep
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

import structlog
from playwright.async_api import Page

from skyvern.cli.core.js_dispatch import raise_if_cancelled
from skyvern.cli.core.page_change import is_page_change_error
from skyvern.cli.core.session_manager import get_current_session
from skyvern.utils.contained_effects import contained_effect

LOG = structlog.get_logger(__name__)
_T = TypeVar("_T")


class _PageWrapper(Protocol):
    page: Page


def _error_message(result: object) -> str:
    if isinstance(result, dict) and result.get("ok") is False:
        error = result.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))
    return ""


async def retry_read_on_page_change(tool: str, page: _PageWrapper, attempt: Callable[[], Awaitable[_T]]) -> _T:
    """The caller must enforce one deadline across all attempts and backoffs.
    Inspect session state directly to avoid reconnecting or selecting a replacement page.
    """
    state = get_current_session()
    raw_page = page.page

    def page_is_current() -> bool:
        raise_if_cancelled()
        selected = state._active_page if state._active_page is not None else state._implicit_page
        return (
            get_current_session() is state
            and not state.selection_lost
            and selected is raw_page
            and not raw_page.is_closed()
        )

    backoff = 0.1
    retry = 0
    while True:
        failure: Exception | None = None
        try:
            result = await attempt()
            raise_if_cancelled()
        except Exception as exc:
            if not is_page_change_error(exc) or not page_is_current():
                raise
            failure = exc
        else:
            if not is_page_change_error(_error_message(result)) or not page_is_current():
                return result

        # The caller's deadline also cancels this wait. Recheck selection afterwards:
        # another tool can switch pages while the failed read is backing off.
        await sleep(backoff)
        if not page_is_current():
            if failure is not None:
                raise failure
            return result
        retry += 1
        with contained_effect("MCP read retried after page change"):
            LOG.info("MCP read retried after page change", tool=tool, retry=retry, backoff_seconds=backoff)
        backoff = min(backoff * 2, 1.0)
