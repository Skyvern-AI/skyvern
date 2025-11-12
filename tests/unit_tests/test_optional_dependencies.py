"""Tests covering optional third-party integrations."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_ddtrace_module() -> object:
    module_name = "skyvern__ddtrace_for_tests"
    module_path = Path(__file__).resolve().parents[2] / "skyvern" / "_ddtrace.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_load_ddtrace_without_dependency(monkeypatch):
    """``load_ddtrace`` should fall back to safe stubs when ddtrace is missing."""

    ddtrace_utils = _load_ddtrace_module()
    original_find_spec = ddtrace_utils.importlib.util.find_spec

    def fake_find_spec(name: str, *args: object, **kwargs: object):  # type: ignore[override]
        if name == "ddtrace":
            return None
        return original_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(ddtrace_utils.importlib.util, "find_spec", fake_find_spec)

    tracer, http_module, trace_filter_cls, span_cls = ddtrace_utils.load_ddtrace()

    assert tracer is None
    assert http_module.URL == "http.url"
    assert trace_filter_cls is ddtrace_utils.NoOpTraceFilter
    assert span_cls is ddtrace_utils.NoOpSpan

    # The fallback filter should simply return the original trace without modification.
    assert trace_filter_cls().process_trace([object()]) is not None
