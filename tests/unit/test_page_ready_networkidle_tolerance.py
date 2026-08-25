import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.utils.page import SkyvernFrame


class _ForeignTimeoutError(Exception):
    """A timeout-like error whose class is not the one the narrow ``except`` names.

    A differently packaged Playwright build can raise a network-idle timeout whose
    class object does not match ``(TimeoutError, asyncio.TimeoutError)``. Readiness
    waits are best-effort, so such a failure must not abort scrape readiness.
    """


class _SelectedError(Exception):
    pass


class _SelectedTimeout(_SelectedError):
    pass


def _selection() -> BrowserEngineSelection:
    selection = MagicMock(spec=BrowserEngineSelection)
    selection.is_engine_error.side_effect = lambda exc: isinstance(exc, _SelectedError)
    selection.is_engine_timeout_error.side_effect = lambda exc: isinstance(exc, _SelectedTimeout)
    return selection


def _isolated_frame(
    side_effect: BaseException,
    engine_selection: BrowserEngineSelection | None = None,
) -> SkyvernFrame:
    frame = AsyncMock()
    frame.wait_for_load_state = AsyncMock(side_effect=side_effect)
    skyvern_frame = SkyvernFrame(frame=frame, engine_selection=engine_selection)
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
async def test_wait_for_page_ready_swallows_selected_engine_timeout(
    span_exporter: InMemorySpanExporter,
) -> None:
    skyvern_frame = _isolated_frame(_SelectedTimeout("timed out"), _selection())

    await skyvern_frame.wait_for_page_ready(network_idle_timeout_ms=10)

    skyvern_frame.frame.wait_for_load_state.assert_awaited_once()
    attrs = _span_attrs(span_exporter, "skyvern.browser.page_ready.network_idle")
    assert attrs.get("result") == "timeout"


@pytest.mark.asyncio
async def test_wait_for_page_ready_swallows_engine_agnostic_runtime_error_under_selected_engine(
    span_exporter: InMemorySpanExporter,
) -> None:
    # evaluate_in_main_world wraps every driver's failure in a plain RuntimeError regardless of
    # engine, so a production-bound selection must still swallow it (matching the sibling
    # navigation-recovery guards) instead of aborting the upload/scrape callers that only catch
    # timeout/PlaywrightError.
    skyvern_frame = _isolated_frame(RuntimeError("main-world evaluate raised: boom"), _selection())

    await skyvern_frame.wait_for_page_ready(network_idle_timeout_ms=10)

    skyvern_frame.frame.wait_for_load_state.assert_awaited_once()
    attrs = _span_attrs(span_exporter, "skyvern.browser.page_ready.network_idle")
    assert attrs.get("result") == "error"


@pytest.mark.asyncio
async def test_wait_for_page_ready_does_not_swallow_cancellation() -> None:
    skyvern_frame = _isolated_frame(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await skyvern_frame.wait_for_page_ready(network_idle_timeout_ms=10)


def _animation_frame(
    side_effect: BaseException,
    engine_selection: BrowserEngineSelection | None = None,
) -> SkyvernFrame:
    frame = AsyncMock()
    frame.wait_for_load_state = AsyncMock(side_effect=side_effect)
    return SkyvernFrame(frame=frame, engine_selection=engine_selection)


@pytest.mark.asyncio
async def test_safe_wait_for_animation_end_swallows_engine_agnostic_runtime_error_under_selected_engine(
    span_exporter: InMemorySpanExporter,
) -> None:
    skyvern_frame = _animation_frame(RuntimeError("main-world evaluate raised: boom"), _selection())

    await skyvern_frame.safe_wait_for_animation_end()

    attrs = _span_attrs(span_exporter, "skyvern.browser.wait_for_animation")
    assert attrs.get("animation_result") == "error"


@pytest.mark.asyncio
async def test_safe_wait_for_animation_end_classifies_selected_engine_timeout(
    span_exporter: InMemorySpanExporter,
) -> None:
    skyvern_frame = _animation_frame(_SelectedTimeout("timed out"), _selection())

    await skyvern_frame.safe_wait_for_animation_end()

    attrs = _span_attrs(span_exporter, "skyvern.browser.wait_for_animation")
    assert attrs.get("animation_result") == "timeout"


@pytest.mark.asyncio
async def test_safe_wait_for_animation_end_does_not_swallow_cancellation_under_selected_engine() -> None:
    skyvern_frame = _animation_frame(asyncio.CancelledError(), _selection())

    with pytest.raises(asyncio.CancelledError):
        await skyvern_frame.safe_wait_for_animation_end()


# -- loading-indicator telemetry: attribution emitted on the existing span (SKY-12170) --

LI_SPAN = "skyvern.browser.page_ready.loading_indicators"

# The full, fixed set of loading_indicator.* attribute keys the span may carry. Privacy and
# cardinality tests assert emitted keys never escape this allowlist.
_ALLOWED_LI_KEYS = {
    "loading_indicator.detected",
    "loading_indicator.poll_count",
    "loading_indicator.selector",
    "loading_indicator.tag",
    "loading_indicator.match_count_bucket",
    "loading_indicator.animated",
    "loading_indicator.determinate",
    "loading_indicator.progress_first",
    "loading_indicator.progress_last",
    "loading_indicator.stable_across_polls",
    "loading_indicator.descriptor_changes",
}


@pytest.fixture
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the 100ms inter-poll sleep so multi-poll sequences run instantly.

    Only ``asyncio.sleep`` is patched; ``asyncio.timeout`` uses loop time and is untouched, so
    the timeout classification path is unaffected.
    """
    from skyvern.webeye.utils import page as page_module

    monkeypatch.setattr(page_module.asyncio, "sleep", AsyncMock())


def _desc(
    sel: int = 0,
    tag: str = "div",
    n: int = 1,
    animated: bool = False,
    determinate: bool | None = None,
    progress: int | None = None,
    ident: tuple[int, int, int, int] = (0, 0, 0, 0),
    **extra: object,
) -> dict:
    descriptor = {
        "sel": sel,
        "tag": tag,
        "n": n,
        "animated": animated,
        "determinate": determinate,
        "progress": progress,
        "ident": list(ident),
    }
    descriptor.update(extra)
    return descriptor


def _loading_frame(
    evaluate_results: list,
    engine_selection: BrowserEngineSelection | None = None,
) -> SkyvernFrame:
    """A frame whose real ``_wait_for_loading_indicators_gone`` runs against a scripted
    ``evaluate`` sequence; the network-idle and DOM-stability steps are inert no-ops."""
    frame = AsyncMock()
    frame.wait_for_load_state = AsyncMock()
    skyvern_frame = SkyvernFrame(frame=frame, engine_selection=engine_selection)
    skyvern_frame._wait_for_dom_stable = AsyncMock()
    skyvern_frame.evaluate = AsyncMock(side_effect=evaluate_results)
    return skyvern_frame


@pytest.mark.asyncio
async def test_loading_indicator_span_excludes_hostile_descriptor_fields(
    fast_poll: None, span_exporter: InMemorySpanExporter
) -> None:
    hostile = _desc(
        sel=3,
        tag="acme-widget",
        className="acme-corp-secret",
        text="ssn 123-45-6789",
        innerHTML="<b>secret</b>",
        dataUrl="https://acme.example.com/private",
    )
    skyvern_frame = _loading_frame([hostile, None])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    li_keys = {key for key in attrs if key.startswith("loading_indicator.")}
    assert li_keys <= _ALLOWED_LI_KEYS
    for value in attrs.values():
        if isinstance(value, str):
            assert "acme-corp-secret" not in value
            assert "ssn" not in value
            assert "acme-widget" not in value
            assert "acme.example.com" not in value
    assert attrs["loading_indicator.detected"] is True
    assert attrs["loading_indicator.selector"] == "class_skeleton"
    assert attrs["loading_indicator.tag"] == "custom"


@pytest.mark.asyncio
async def test_loading_indicator_fields_are_bounded(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    out_of_range = _desc(sel=99, tag="div", n=500, progress=42)
    skyvern_frame = _loading_frame([out_of_range, None])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["loading_indicator.selector"] == "unknown"
    assert attrs["loading_indicator.match_count_bucket"] == "6+"
    assert 0 <= attrs["loading_indicator.progress_last"] <= 10


@pytest.mark.asyncio
async def test_descriptor_changes_capped_at_ten(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    sequence: list = [_desc(sel=0, tag="div", progress=index % 2) for index in range(14)]
    sequence.append(None)
    skyvern_frame = _loading_frame(sequence)

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["loading_indicator.descriptor_changes"] == 10
    assert attrs["loading_indicator.stable_across_polls"] is False


@pytest.mark.asyncio
async def test_animated_spinner_timeout_is_attributed(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    animated = _desc(sel=0, tag="div", animated=True)
    skyvern_frame = _loading_frame([animated, animated, TimeoutError()])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["result"] == "timeout"
    assert attrs["loading_indicator.detected"] is True
    assert attrs["loading_indicator.animated"] is True
    assert attrs["loading_indicator.selector"] == "class_spinner"


@pytest.mark.asyncio
async def test_static_skeleton_timeout_is_stable(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    skeleton = _desc(sel=3, tag="div", animated=False, ident=(1, 1, 2, 2))
    skyvern_frame = _loading_frame([skeleton, skeleton, skeleton, TimeoutError()])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["result"] == "timeout"
    assert attrs["loading_indicator.selector"] == "class_skeleton"
    assert attrs["loading_indicator.animated"] is False
    assert attrs["loading_indicator.stable_across_polls"] is True


@pytest.mark.asyncio
async def test_static_progress_tracker_timeout_is_stable(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    tracker = _desc(sel=4, tag="ul", animated=False)
    skyvern_frame = _loading_frame([tracker, tracker, TimeoutError()])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["result"] == "timeout"
    assert attrs["loading_indicator.selector"] == "class_progress"
    assert attrs["loading_indicator.animated"] is False
    assert attrs["loading_indicator.stable_across_polls"] is True


@pytest.mark.asyncio
async def test_determinate_progress_advancement_is_recorded(
    fast_poll: None, span_exporter: InMemorySpanExporter
) -> None:
    sequence = [
        _desc(sel=6, tag="progress", determinate=True, progress=2, ident=(0, 0, 1, 1)),
        _desc(sel=6, tag="progress", determinate=True, progress=5, ident=(0, 0, 1, 1)),
        _desc(sel=6, tag="progress", determinate=True, progress=7, ident=(0, 0, 1, 1)),
        None,
    ]
    skyvern_frame = _loading_frame(sequence)

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["result"] == "success"
    assert attrs["loading_indicator.determinate"] == "true"
    assert attrs["loading_indicator.progress_first"] == 2
    assert attrs["loading_indicator.progress_last"] == 7
    assert attrs["loading_indicator.stable_across_polls"] is False


@pytest.mark.asyncio
async def test_indeterminate_progress_emits_no_progress_attrs(
    fast_poll: None, span_exporter: InMemorySpanExporter
) -> None:
    indeterminate = _desc(sel=6, tag="div", determinate=False, progress=None)
    skyvern_frame = _loading_frame([indeterminate, None])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["loading_indicator.determinate"] == "false"
    assert "loading_indicator.progress_first" not in attrs
    assert "loading_indicator.progress_last" not in attrs


@pytest.mark.asyncio
async def test_stale_aria_busy_signature(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    aria_busy = _desc(sel=8, tag="div", animated=False)
    skyvern_frame = _loading_frame([aria_busy, aria_busy, TimeoutError()])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["result"] == "timeout"
    assert attrs["loading_indicator.selector"] == "aria_busy"
    assert attrs["loading_indicator.animated"] is False
    assert attrs["loading_indicator.stable_across_polls"] is True


@pytest.mark.asyncio
async def test_indicator_clears_mid_wait_is_success(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    descriptor = _desc()
    skyvern_frame = _loading_frame([descriptor, descriptor, None])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["result"] == "success"
    assert attrs["loading_indicator.detected"] is True
    assert attrs["loading_indicator.poll_count"] == 3


@pytest.mark.asyncio
async def test_indicator_never_detected(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    skyvern_frame = _loading_frame([None])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["result"] == "success"
    assert attrs["loading_indicator.detected"] is False
    assert attrs["loading_indicator.poll_count"] == 1
    assert "loading_indicator.selector" not in attrs


@pytest.mark.asyncio
async def test_navigation_context_destroyed_keeps_partial_observation(
    fast_poll: None, span_exporter: InMemorySpanExporter
) -> None:
    from playwright._impl._errors import Error as PlaywrightError

    descriptor = _desc(sel=0, tag="div")
    skyvern_frame = _loading_frame(
        [descriptor, PlaywrightError("Execution context was destroyed, most likely because of a navigation")]
    )

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["result"] == "error"
    assert attrs["loading_indicator.detected"] is True
    assert attrs["loading_indicator.selector"] == "class_spinner"


@pytest.mark.asyncio
async def test_detached_frame_keeps_partial_observation(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    from playwright._impl._errors import Error as PlaywrightError

    descriptor = _desc(sel=0, tag="div")
    skyvern_frame = _loading_frame([descriptor, PlaywrightError("Frame was detached")])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["result"] == "error"
    assert attrs["loading_indicator.detected"] is True


@pytest.mark.asyncio
async def test_repeated_waits_do_not_bleed_observation_state(
    fast_poll: None, span_exporter: InMemorySpanExporter
) -> None:
    frame = AsyncMock()
    frame.wait_for_load_state = AsyncMock()
    skyvern_frame = SkyvernFrame(frame=frame)
    skyvern_frame._wait_for_dom_stable = AsyncMock()
    skyvern_frame.evaluate = AsyncMock(side_effect=[_desc(sel=0), None, None])

    await skyvern_frame.wait_for_page_ready()
    span_exporter.clear()
    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    assert attrs["loading_indicator.detected"] is False
    assert attrs["loading_indicator.poll_count"] == 1
    assert "loading_indicator.selector" not in attrs


@pytest.mark.asyncio
async def test_malformed_truthy_descriptor_is_tolerated(fast_poll: None, span_exporter: InMemorySpanExporter) -> None:
    skyvern_frame = _loading_frame([True, "loading", None])

    await skyvern_frame.wait_for_page_ready()

    attrs = _span_attrs(span_exporter, LI_SPAN)
    li_keys = {key for key in attrs if key.startswith("loading_indicator.")}
    assert li_keys <= _ALLOWED_LI_KEYS
    assert attrs["result"] == "success"
    assert attrs["loading_indicator.detected"] is True
    assert attrs["loading_indicator.selector"] == "unknown"
    assert attrs["loading_indicator.tag"] == "custom"


@pytest.mark.asyncio
async def test_loading_indicators_gone_still_raises_timeout_on_expiry() -> None:
    # Locks the raise contract the upload/scrape callers classify on; the optional observation
    # kwarg must default to None so existing AsyncMock overrides keep working.
    frame = AsyncMock()
    skyvern_frame = SkyvernFrame(frame=frame)
    skyvern_frame.evaluate = AsyncMock(return_value=_desc())

    with pytest.raises(TimeoutError):
        await skyvern_frame._wait_for_loading_indicators_gone(timeout_ms=30)
