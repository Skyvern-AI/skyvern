"""Span events are a reporting path with no logging processors in front of them.

``Span.record_exception`` renders ``str(exc)`` and the traceback into ``exception.message`` /
``exception.stacktrace`` and ships them as span attributes, so a credential inside the exception
text reaches the exporter with nothing in its way.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.forge.sdk.copilot import secret_scrub
from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER
from skyvern.forge.sdk.trace import record_span_exception, traced_span

_REGISTERED_CREDENTIAL = "fake-span-pa55w0rd-4b7e2d"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def registered_credential() -> Iterator[str]:
    secret_scrub._SESSION_SCRUB_VALUES.clear()
    secret_scrub._SESSION_SCRUB_VALUES["pbs_span"] = [_REGISTERED_CREDENTIAL]
    try:
        yield _REGISTERED_CREDENTIAL
    finally:
        secret_scrub._SESSION_SCRUB_VALUES.clear()


def _drain(exporter: InMemorySpanExporter) -> tuple[str, list[str | None]]:
    finished = exporter.get_finished_spans()
    events_blob = "".join(str(ev.attributes) for s in finished for ev in s.events)
    return events_blob, [s.status.description for s in finished]


def _record_and_export(credential: str, *, set_error_status: bool = True) -> tuple[str, list[str | None]]:
    """The helper on its own, for a span the caller handles without re-raising."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with traced_span(provider.get_tracer("test"), "probe") as span:
        try:
            raise RuntimeError(f"(psycopg.errors) INSERT failed [parameters: ('{credential}',)]")
        except RuntimeError as exc:
            record_span_exception(span, exc, set_error_status=set_error_status)
    provider.force_flush()
    return _drain(exporter)


def _raise_through_span(credential: str) -> tuple[str, list[str | None]]:
    """The production shape: the exception PROPAGATES out of the span context.

    This is what `@traced` and the inline `traced_span` sites do. OTel's `use_span` records the
    exception on the way out, so a span left at the default `record_exception=True` appends a
    second, unscrubbed event and overwrites the scrubbed status.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with pytest.raises(RuntimeError):
        with traced_span(provider.get_tracer("test"), "probe"):
            raise RuntimeError(f"(psycopg.errors) INSERT failed [parameters: ('{credential}',)]")
    provider.force_flush()
    return _drain(exporter)


def test_credential_is_redacted_from_the_span_exception_event(registered_credential: str) -> None:
    events, _ = _record_and_export(registered_credential)
    assert registered_credential not in events
    assert REDACTED_SECRET_PLACEHOLDER in events


def test_credential_is_redacted_from_the_span_error_status(registered_credential: str) -> None:
    _, descriptions = _record_and_export(registered_credential)
    assert descriptions and all(d is not None for d in descriptions)
    blob = "".join(d or "" for d in descriptions)
    assert registered_credential not in blob
    assert REDACTED_SECRET_PLACEHOLDER in blob


def test_span_exception_stays_useful_for_debugging(registered_credential: str) -> None:
    events, _ = _record_and_export(registered_credential)
    assert "RuntimeError" in events
    assert "psycopg.errors" in events
    assert "Traceback (most recent call last)" in events
    assert "_record_and_export" in events


def test_status_is_left_alone_when_not_requested(registered_credential: str) -> None:
    """The copilot turn span records the exception without claiming the span failed."""
    _, descriptions = _record_and_export(registered_credential, set_error_status=False)
    assert descriptions == [None]


def test_propagating_exception_is_redacted_and_recorded_once(registered_credential: str) -> None:
    """Regression for the use_span double-record: exactly one event, and it is scrubbed."""
    events, descriptions = _raise_through_span(registered_credential)

    assert events.count("exception.stacktrace") == 1, "use_span appended a second exception event"
    assert registered_credential not in events
    assert registered_credential not in "".join(d or "" for d in descriptions)
    assert REDACTED_SECRET_PLACEHOLDER in events


def test_propagating_exception_stays_useful_for_debugging(registered_credential: str) -> None:
    events, descriptions = _raise_through_span(registered_credential)

    assert "RuntimeError" in events
    assert "psycopg.errors" in events
    assert "Traceback (most recent call last)" in events
    assert any(d and d.startswith("RuntimeError: ") for d in descriptions)


def _bare_span_start_sites() -> list[str]:
    """Every bare ``*.start_as_current_span(...)`` outside the helper, found via AST not grep."""
    offenders: list[str] = []
    helper = _REPO_ROOT / "skyvern" / "forge" / "sdk" / "trace" / "__init__.py"
    for package in ("skyvern", "cloud"):
        root = _REPO_ROOT / package
        if not root.is_dir():
            # This file is synced to the OSS repo, where `cloud/` does not exist. Skipping
            # explicitly rather than relying on rglob() silently yielding nothing on a missing dir.
            continue
        for path in root.rglob("*.py"):
            if path == helper:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "start_as_current_span"
                ):
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
    return sorted(offenders)


def test_no_bare_start_as_current_span_calls_remain() -> None:
    """A bare span start re-opens the leak: use_span records the raw exception on the way out.

    Use ``traced_span`` instead, which disables that and records the scrubbed exception itself.
    """
    assert _bare_span_start_sites() == []


def _bare_record_exception_sites() -> list[str]:
    """Every ``*.record_exception(...)`` call outside the helper, found via AST not grep."""
    offenders: list[str] = []
    helper = _REPO_ROOT / "skyvern" / "forge" / "sdk" / "trace" / "__init__.py"
    for package in ("skyvern", "cloud"):
        root = _REPO_ROOT / package
        if not root.is_dir():
            # This file is synced to the OSS repo, where `cloud/` does not exist. Skipping
            # explicitly rather than relying on rglob() silently yielding nothing on a missing dir.
            continue
        for path in root.rglob("*.py"):
            if path == helper:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "record_exception"
                ):
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
    return sorted(offenders)


def test_no_bare_record_exception_calls_remain() -> None:
    """Remove the capability to err: a bare ``record_exception`` re-opens the leak silently.

    Use ``record_span_exception`` instead. If a call genuinely must bypass scrubbing, widen the
    helper rather than this guard.
    """
    assert _bare_record_exception_sites() == []


def test_the_guard_can_actually_fail(tmp_path: Path) -> None:
    """Non-vacuity: the AST walk must flag a bare call, not pass because it found nothing."""
    offending = tmp_path / "offender.py"
    offending.write_text("def f(span, exc):\n    span.record_exception(exc)\n", encoding="utf-8")
    tree = ast.parse(offending.read_text(encoding="utf-8"))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "record_exception"
    ]
    assert len(found) == 1
