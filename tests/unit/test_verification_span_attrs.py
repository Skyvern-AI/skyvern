"""``record_verification_span_attrs`` writes ``verification.reasoning_kind`` on the verify span (SKY-9174)."""

from __future__ import annotations

import opentelemetry.trace as otel_trace
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.forge.agent import record_verification_span_attrs

SPAN_NAME = "skyvern.agent.verification_fixture"


@pytest.mark.parametrize(
    "thoughts, expected",
    [
        pytest.param(
            "The page does not contain the exact phrase 'Your message has been sent'.", "literal", id="literal"
        ),
        pytest.param(
            "The page renders a thank-you confirmation, satisfying the goal's intent.", "semantic", id="semantic"
        ),
        pytest.param(None, "semantic", id="none-defaults-to-semantic"),
    ],
)
def test_verification_span_records_the_reasoning_kind(
    thoughts: str | None, expected: str, span_exporter: InMemorySpanExporter
) -> None:
    tracer = otel_trace.get_tracer("verification-span-attrs-test")
    with tracer.start_as_current_span(SPAN_NAME) as span:
        record_verification_span_attrs(span, thoughts)

    span_record = next(s for s in span_exporter.get_finished_spans() if s.name == SPAN_NAME)
    assert dict(span_record.attributes or {}).get("verification.reasoning_kind") == expected
