"""Tests for animation wait span attribute instrumentation."""

import ast
import inspect
import os
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.webeye.utils.page import SkyvernFrame


def test_safe_wait_caller_defaults_to_unknown() -> None:
    sig = inspect.signature(SkyvernFrame.safe_wait_for_animation_end)
    assert "caller" in sig.parameters
    assert sig.parameters["caller"].default == "unknown"


@pytest.mark.asyncio
async def test_safe_wait_for_animation_end_classifies_skyvern_analysis_timeout(
    span_exporter: InMemorySpanExporter,
) -> None:
    frame = AsyncMock()
    frame.wait_for_load_state = AsyncMock()
    skyvern_frame = SkyvernFrame(frame=frame)
    skyvern_frame.wait_for_animation_end = AsyncMock(
        side_effect=SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")
    )

    await skyvern_frame.safe_wait_for_animation_end(caller="test")

    span = next(
        (span for span in span_exporter.get_finished_spans() if span.name == "skyvern.browser.wait_for_animation"),
        None,
    )
    assert span is not None
    assert (span.attributes or {}).get("animation_result") == "timeout"


def test_all_production_call_sites_pass_caller() -> None:
    """Every safe_wait_for_animation_end call in production code must include caller=.

    Uses AST parsing so multi-line / black-wrapped calls are handled correctly.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    scan_dirs = [os.path.join(repo_root, d) for d in ("skyvern", "cloud", "scripts")]
    missing: list[str] = []

    for scan_dir in scan_dirs:
        if not os.path.isdir(scan_dir):
            continue
        for dirpath, _, filenames in os.walk(scan_dir):
            if "__pycache__" in dirpath or ".venv" in dirpath:
                continue
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    with open(fpath) as f:
                        content = f.read()
                except OSError:
                    continue
                # A call site must textually contain the method name; skip parsing
                # the thousands of files that cannot possibly hold one.
                if "safe_wait_for_animation_end" not in content:
                    continue
                try:
                    tree = ast.parse(content, filename=fpath)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Await):
                        continue
                    call = node.value
                    if not isinstance(call, ast.Call):
                        continue
                    func = call.func
                    is_target = isinstance(func, ast.Attribute) and func.attr == "safe_wait_for_animation_end"
                    if not is_target:
                        continue
                    has_caller = any(kw.arg == "caller" for kw in call.keywords)
                    if not has_caller:
                        rel = os.path.relpath(fpath, repo_root)
                        missing.append(f"{rel}:{node.lineno}")

    assert not missing, "Call sites missing caller= keyword:\n" + "\n".join(f"  {m}" for m in missing)
