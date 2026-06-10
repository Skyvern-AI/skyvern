"""Coarse observability attrs on the ``skyvern.agent.scrape`` span.

Pure-unit coverage of ``_record_scrape_span_attrs``: a real tracer + the shared
``span_exporter`` fixture, no browser, no Playwright. Asserts the input/mode,
output-shape, caller-bucket, and outcome attrs are emitted under both an
attributed (``scrape_trigger`` set) and an unattributed (fallback) caller.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.scraper.scraper import _record_scrape_span_attrs

SCRAPE_SPAN_NAME = "skyvern.agent.scrape"


def _span_by_name(spans, name):
    return next((s for s in spans if s.name == name), None)


def _record_inside_span(**kwargs) -> None:
    tracer = otel_trace.get_tracer("skyvern")
    with tracer.start_as_current_span(SCRAPE_SPAN_NAME):
        _record_scrape_span_attrs(**kwargs)


_BASE_KWARGS = dict(
    elements=[{"id": "x"}, {"id": "y"}],
    html="<html></html>",
    text_content="hello world",
    url="https://example.com/path?secret=abc",
    take_screenshots=True,
    draw_boxes=False,
    scroll=True,
    max_screenshot_number=3,
    screenshots=[b"aaa", b"bbbb"],
    id_to_frame_dict={"f1": object(), "f2": object(), "f3": object()},
    empty_page_retry=False,
)


def test_scrape_span_records_mode_output_and_unspecified_trigger_when_no_context(
    span_exporter: InMemorySpanExporter,
) -> None:
    skyvern_context.reset()
    _record_inside_span(**_BASE_KWARGS)

    span = _span_by_name(span_exporter.get_finished_spans(), SCRAPE_SPAN_NAME)
    assert span is not None
    attrs = span.attributes or {}

    # Output shape (cheap signals only)
    assert attrs.get("element_count") == 2
    assert attrs.get("html_bytes") == len("<html></html>")
    assert attrs.get("text_bytes") == len("hello world")
    assert attrs.get("screenshot_count") == 2
    assert attrs.get("screenshot_bytes") == len(b"aaa") + len(b"bbbb")
    assert attrs.get("frame_count") == 3

    # Input/mode params
    assert attrs.get("take_screenshots") is True
    assert attrs.get("draw_boxes") is False
    assert attrs.get("scroll") is True
    assert attrs.get("max_screenshot_number") == 3

    # Outcome
    assert attrs.get("empty_page_retry") is False

    # Caller bucket: unspecified when no SkyvernContext is active
    assert attrs.get("scrape_trigger") == "unspecified"
    # screenshots_consumed is intentionally absent when the caller did not set it
    assert "screenshots_consumed" not in attrs

    # URL is stripped of query params to bound cardinality
    page_url = attrs.get("page_url")
    assert page_url is not None
    assert "secret" not in page_url


def test_scrape_span_uses_context_trigger_when_caller_sets_it(
    span_exporter: InMemorySpanExporter,
) -> None:
    ctx = SkyvernContext(tz_info=ZoneInfo("UTC"))
    ctx.scrape_trigger = "verification"
    ctx.scrape_screenshots_consumed = True
    skyvern_context.set(ctx)
    try:
        _record_inside_span(**_BASE_KWARGS)
    finally:
        skyvern_context.reset()

    span = _span_by_name(span_exporter.get_finished_spans(), SCRAPE_SPAN_NAME)
    assert span is not None
    attrs = span.attributes or {}
    assert attrs.get("scrape_trigger") == "verification"
    assert attrs.get("screenshots_consumed") is True


def test_scrape_span_marks_empty_page_retry_when_set(
    span_exporter: InMemorySpanExporter,
) -> None:
    kwargs = dict(_BASE_KWARGS)
    kwargs["empty_page_retry"] = True
    skyvern_context.reset()
    _record_inside_span(**kwargs)

    span = _span_by_name(span_exporter.get_finished_spans(), SCRAPE_SPAN_NAME)
    assert span is not None
    attrs = span.attributes or {}
    assert attrs.get("empty_page_retry") is True


@pytest.mark.parametrize("html_value, expected_bytes", [("", 0), ("abc", 3)])
def test_scrape_span_handles_empty_html(
    span_exporter: InMemorySpanExporter, html_value: str, expected_bytes: int
) -> None:
    kwargs = dict(_BASE_KWARGS)
    kwargs["html"] = html_value
    skyvern_context.reset()
    _record_inside_span(**kwargs)

    span = _span_by_name(span_exporter.get_finished_spans(), SCRAPE_SPAN_NAME)
    assert span is not None
    attrs = span.attributes or {}
    assert attrs.get("html_bytes") == expected_bytes
