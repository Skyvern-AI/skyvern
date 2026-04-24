"""Tests for the ``validation.decision`` / ``validation.reasoning_kind`` span
attributes (SKY-9174, Part D.3).

The two attributes give us a post-merge logfire signal for when a validation
block's LLM reasons literally and/or terminates — the failure mode Part D aims
to reduce. Query shape::

    SELECT COUNT(*) FROM records
    WHERE span_name = 'skyvern.agent.step_body'
      AND attributes->>'validation.decision' = 'terminate'
      AND attributes->>'validation.reasoning_kind' = 'literal'
      AND start_timestamp > now() - INTERVAL '24 hours';

Pre-fix this count should be non-trivial; post-fix it should trend to zero on
the copilot-v2 cohort. These tests cover the attribute-writing logic directly
(the helper is pure, so we don't need to drive the full agent step).
"""

from __future__ import annotations

from datetime import UTC, datetime

import opentelemetry.trace as otel_trace
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.forge.agent import record_validation_span_attrs
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    ClickAction,
    CompleteAction,
    TerminateAction,
)
from tests.unit.helpers import make_organization, make_task

STEP_SPAN_NAME = "skyvern.agent.validation_step_body_fixture"


def _validation_task() -> object:
    now = datetime.now(UTC)
    org = make_organization(now)
    return make_task(now, org, task_type=TaskType.validation)


def _general_task() -> object:
    now = datetime.now(UTC)
    org = make_organization(now)
    return make_task(now, org, task_type=TaskType.general)


def _run_with_span(task: object, actions: list[Action]) -> dict:
    """Start a span, invoke the helper inside it, end the span. Return the
    span's attribute dict via the in-memory exporter."""
    tracer = otel_trace.get_tracer("sky-9174-test")
    with tracer.start_as_current_span(STEP_SPAN_NAME) as span:
        record_validation_span_attrs(span, task, actions)
    return {}  # attrs read from the exporter by the caller


def _span_attrs(span_exporter: InMemorySpanExporter) -> dict:
    span = next((s for s in span_exporter.get_finished_spans() if s.name == STEP_SPAN_NAME), None)
    assert span is not None, "expected fixture span to be recorded"
    return dict(span.attributes or {})


def _complete_action(reasoning: str) -> CompleteAction:
    return CompleteAction(
        reasoning=reasoning,
        intention=reasoning,
        action_type=ActionType.COMPLETE,
    )


def _terminate_action(reasoning: str) -> TerminateAction:
    return TerminateAction(
        reasoning=reasoning,
        intention=reasoning,
        action_type=ActionType.TERMINATE,
    )


def test_complete_with_semantic_reasoning_records_semantic(span_exporter: InMemorySpanExporter) -> None:
    task = _validation_task()
    actions = [_complete_action("The current page shows a thank-you confirmation.")]
    _run_with_span(task, actions)
    attrs = _span_attrs(span_exporter)
    assert attrs.get("validation.decision") == "complete"
    assert attrs.get("validation.reasoning_kind") == "semantic"


def test_terminate_with_literal_reasoning_records_literal(span_exporter: InMemorySpanExporter) -> None:
    """The regression we care about most: LLM terminated because an exact
    string wasn't found. This is the combination (terminate, literal) that
    Part D aims to drive toward zero."""
    task = _validation_task()
    actions = [
        _terminate_action(
            "The page does not contain the exact complete-criterion text 'Your message has been sent'. TERMINATE."
        )
    ]
    _run_with_span(task, actions)
    attrs = _span_attrs(span_exporter)
    assert attrs.get("validation.decision") == "terminate"
    assert attrs.get("validation.reasoning_kind") == "literal"


def test_terminate_with_semantic_reasoning_records_semantic(span_exporter: InMemorySpanExporter) -> None:
    task = _validation_task()
    actions = [_terminate_action("An error banner surfaced at the top of the page saying the submission failed.")]
    _run_with_span(task, actions)
    attrs = _span_attrs(span_exporter)
    assert attrs.get("validation.decision") == "terminate"
    assert attrs.get("validation.reasoning_kind") == "semantic"


def test_complete_with_literal_reasoning_records_literal(span_exporter: InMemorySpanExporter) -> None:
    """Symmetric — a literal COMPLETE is harmless but we still tag it, because
    the post-merge dashboard cares about the distribution across both axes,
    not just the terminate one."""
    task = _validation_task()
    actions = [_complete_action("The page contains the exact phrase 'Your message has been sent'.")]
    _run_with_span(task, actions)
    attrs = _span_attrs(span_exporter)
    assert attrs.get("validation.decision") == "complete"
    assert attrs.get("validation.reasoning_kind") == "literal"


def test_non_validation_task_does_not_tag_span(span_exporter: InMemorySpanExporter) -> None:
    """Guard against accidental tagging of non-validation step spans — those
    span attributes are reserved for TaskType.validation."""
    task = _general_task()
    actions = [_complete_action("The current page shows a thank-you confirmation.")]
    _run_with_span(task, actions)
    attrs = _span_attrs(span_exporter)
    assert "validation.decision" not in attrs
    assert "validation.reasoning_kind" not in attrs


def test_non_decisive_action_does_not_tag_span(span_exporter: InMemorySpanExporter) -> None:
    """Validation tasks whose first action isn't a Complete/Terminate (unusual
    but possible during partial parsing) should not produce tagged attrs."""
    task = _validation_task()
    # A ClickAction stands in for any non-DecisiveAction leading-first.
    non_decisive = ClickAction(action_type=ActionType.CLICK, element_id="AAAB", reasoning="click")
    _run_with_span(task, [non_decisive])
    attrs = _span_attrs(span_exporter)
    assert "validation.decision" not in attrs
    assert "validation.reasoning_kind" not in attrs


def test_empty_actions_list_does_not_tag_span(span_exporter: InMemorySpanExporter) -> None:
    task = _validation_task()
    _run_with_span(task, [])
    attrs = _span_attrs(span_exporter)
    assert "validation.decision" not in attrs
    assert "validation.reasoning_kind" not in attrs


def test_missing_reasoning_defaults_to_semantic(span_exporter: InMemorySpanExporter) -> None:
    """Empty/None reasoning shouldn't crash — absence of literal signals means
    semantic by the helper's rule."""
    task = _validation_task()
    actions = [_complete_action("")]
    _run_with_span(task, actions)
    attrs = _span_attrs(span_exporter)
    assert attrs.get("validation.decision") == "complete"
    assert attrs.get("validation.reasoning_kind") == "semantic"


@pytest.mark.parametrize(
    "signal",
    ["exact", "literal", "verbatim", "word-for-word", "word for word"],
)
def test_every_literal_signal_flags_reasoning(signal: str, span_exporter: InMemorySpanExporter) -> None:
    """Each configured signal, on its own, must classify reasoning as literal."""
    task = _validation_task()
    actions = [_terminate_action(f"The criterion does not appear {signal} on the page.")]
    _run_with_span(task, actions)
    attrs = _span_attrs(span_exporter)
    assert attrs.get("validation.reasoning_kind") == "literal", signal
