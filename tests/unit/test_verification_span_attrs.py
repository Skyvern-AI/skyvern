"""Tests for the ``verification.reasoning_kind`` span attribute (SKY-9174, Part E.2).

Part A already wrote ``verification.status`` and ``verification.template`` on
``skyvern.agent.complete_verify`` spans. Part E adds one more:
``verification.reasoning_kind`` (``literal`` | ``semantic``) derived from the
verifier LLM's ``thoughts`` text via the shared ``_classify_reasoning_kind``
heuristic. Post-fix logfire query::

    SELECT COUNT(*) FROM records
    WHERE span_name = 'skyvern.agent.complete_verify'
      AND attributes->>'verification.status' != 'complete'
      AND attributes->>'verification.reasoning_kind' = 'literal'
      AND start_timestamp > now() - INTERVAL '24 hours';

Pre-fix, a single reproducing run produces double-digit rows with
``reasoning_kind = 'literal'``. Post-fix, that count should trend to zero.
"""

from __future__ import annotations

import opentelemetry.trace as otel_trace
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.forge.agent import record_verification_span_attrs

SPAN_NAME = "skyvern.agent.verification_fixture"


def _run_with_span(thoughts: str | None) -> None:
    tracer = otel_trace.get_tracer("sky-9174-part-e-test")
    with tracer.start_as_current_span(SPAN_NAME) as span:
        record_verification_span_attrs(span, thoughts)


def _span_attrs(span_exporter: InMemorySpanExporter) -> dict:
    span = next((s for s in span_exporter.get_finished_spans() if s.name == SPAN_NAME), None)
    assert span is not None, "expected fixture span to be recorded"
    return dict(span.attributes or {})


def test_literal_reasoning_records_literal(span_exporter: InMemorySpanExporter) -> None:
    """The regression we care about most: verifier insisted on finding the
    criterion's exact wording on the page and returned ``continue``. This is
    the (verifier, literal) combination Part E aims to drive toward zero."""
    _run_with_span("The page does not contain the exact phrase 'Your message has been sent'. user_goal_achieved=false.")
    attrs = _span_attrs(span_exporter)
    assert attrs.get("verification.reasoning_kind") == "literal"


def test_semantic_reasoning_records_semantic(span_exporter: InMemorySpanExporter) -> None:
    _run_with_span("The page renders a thank-you confirmation, satisfying the goal's intent.")
    attrs = _span_attrs(span_exporter)
    assert attrs.get("verification.reasoning_kind") == "semantic"


def test_empty_reasoning_defaults_to_semantic(span_exporter: InMemorySpanExporter) -> None:
    _run_with_span("")
    attrs = _span_attrs(span_exporter)
    assert attrs.get("verification.reasoning_kind") == "semantic"


def test_none_reasoning_defaults_to_semantic(span_exporter: InMemorySpanExporter) -> None:
    _run_with_span(None)
    attrs = _span_attrs(span_exporter)
    assert attrs.get("verification.reasoning_kind") == "semantic"


@pytest.mark.parametrize(
    "signal",
    ["exact", "literal", "verbatim", "word-for-word", "word for word"],
)
def test_every_literal_signal_flags_reasoning(signal: str, span_exporter: InMemorySpanExporter) -> None:
    """Shared classifier: each signal used by ``record_validation_span_attrs``
    also flags verifier reasoning. Guards against future drift between the two
    callers."""
    _run_with_span(f"The goal does not appear {signal} on the page.")
    attrs = _span_attrs(span_exporter)
    assert attrs.get("verification.reasoning_kind") == "literal", signal
