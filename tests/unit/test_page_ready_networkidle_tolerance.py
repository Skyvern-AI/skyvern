import asyncio
from unittest.mock import AsyncMock

import pytest

from skyvern.webeye.utils.page import SkyvernFrame


class _ForeignTimeoutError(Exception):
    """A timeout-like error whose class is not the one the narrow ``except`` names.

    A differently packaged Playwright build can raise a network-idle timeout whose
    class object does not match ``(TimeoutError, asyncio.TimeoutError)``. Readiness
    waits are best-effort, so such a failure must not abort scrape readiness.
    """


def _isolated_frame(side_effect: BaseException) -> SkyvernFrame:
    frame = AsyncMock()
    frame.wait_for_load_state = AsyncMock(side_effect=side_effect)
    skyvern_frame = SkyvernFrame(frame=frame)
    # Isolate the network-idle step; the other two readiness checks are no-ops here.
    skyvern_frame._wait_for_loading_indicators_gone = AsyncMock()
    skyvern_frame._wait_for_dom_stable = AsyncMock()
    return skyvern_frame


@pytest.mark.asyncio
async def test_wait_for_page_ready_swallows_foreign_networkidle_error() -> None:
    skyvern_frame = _isolated_frame(_ForeignTimeoutError("Timeout 3000.0ms exceeded."))

    # Must return without raising, matching the loading-indicator and DOM-stability branches.
    await skyvern_frame.wait_for_page_ready(network_idle_timeout_ms=10)

    skyvern_frame.frame.wait_for_load_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_page_ready_swallows_builtin_networkidle_timeout() -> None:
    skyvern_frame = _isolated_frame(TimeoutError("Timeout 3000.0ms exceeded."))

    await skyvern_frame.wait_for_page_ready(network_idle_timeout_ms=10)

    skyvern_frame.frame.wait_for_load_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_page_ready_does_not_swallow_cancellation() -> None:
    skyvern_frame = _isolated_frame(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await skyvern_frame.wait_for_page_ready(network_idle_timeout_ms=10)
