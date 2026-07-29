import asyncio
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.exceptions import SkyvernPageAnalysisTimeout
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


def _span_attrs(span_exporter: InMemorySpanExporter, name: str) -> dict:
    span = next((span for span in span_exporter.get_finished_spans() if span.name == name), None)
    assert span is not None
    return dict(span.attributes or {})


@pytest.mark.asyncio
async def test_wait_for_page_ready_classifies_loading_indicator_skyvern_analysis_timeout(
    span_exporter: InMemorySpanExporter,
) -> None:
    skyvern_frame = _isolated_frame(Exception())
    skyvern_frame.frame.wait_for_load_state = AsyncMock()
    skyvern_frame._wait_for_loading_indicators_gone = AsyncMock(
        side_effect=SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")
    )

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, "skyvern.browser.page_ready.loading_indicators")
    assert attrs.get("result") == "timeout"


@pytest.mark.asyncio
async def test_wait_for_page_ready_classifies_dom_stability_skyvern_analysis_timeout(
    span_exporter: InMemorySpanExporter,
) -> None:
    skyvern_frame = _isolated_frame(Exception())
    skyvern_frame.frame.wait_for_load_state = AsyncMock()
    skyvern_frame._wait_for_dom_stable = AsyncMock(
        side_effect=SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")
    )

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, "skyvern.browser.page_ready.dom_stability")
    assert attrs.get("result") == "timeout"


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
