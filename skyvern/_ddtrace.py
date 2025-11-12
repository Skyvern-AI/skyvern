"""Utilities for working with the optional ddtrace dependency."""

from __future__ import annotations

import importlib
import importlib.util
from types import SimpleNamespace
from typing import Any, Tuple, Type


class NoOpSpan:
    """Fallback Span implementation used when ddtrace is unavailable."""

    parent_id: Any = None

    def get_tag(self, _: str) -> Any:  # pragma: no cover - trivial
        return None


class NoOpTraceFilter:
    """Fallback implementation used when ddtrace is not installed."""

    def process_trace(self, trace: list[NoOpSpan]) -> list[NoOpSpan] | None:  # pragma: no cover - trivial
        return trace


def load_ddtrace() -> Tuple[Any | None, Any, Type, Type]:
    """Load ddtrace components if the dependency is available.

    Returns a tuple containing the tracer instance (or ``None``), the http module,
    and the TraceFilter and Span base classes to inherit from.
    """

    ddtrace_spec = importlib.util.find_spec("ddtrace")
    if ddtrace_spec is None:
        return None, SimpleNamespace(URL="http.url"), NoOpTraceFilter, NoOpSpan

    ddtrace = importlib.import_module("ddtrace")
    http_module = importlib.import_module("ddtrace.ext.http")
    trace_module = importlib.import_module("ddtrace.trace")
    return ddtrace.tracer, http_module, trace_module.TraceFilter, trace_module.Span


__all__ = [
    "NoOpSpan",
    "NoOpTraceFilter",
    "load_ddtrace",
]
