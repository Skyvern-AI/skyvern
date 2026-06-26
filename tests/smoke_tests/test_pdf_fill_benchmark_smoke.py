"""Real-LLM smoke gate for the pdf_fill benchmark.

Skipped by default (hermetic CI). Set RUN_PDF_FILL_BENCHMARK=1 with a local backend env that
can serve the LLM to run a bigger subset against the real model:

    RUN_PDF_FILL_BENCHMARK=1 PDF_FILL_BENCHMARK_ORG=o_... uv run pytest tests/smoke_tests/test_pdf_fill_benchmark_smoke.py -s

The curated cases are treated as a must-pass gate; the round-trip accuracy is an informational
threshold (the exploratory corpus is for finding opportunities — see the benchmark README).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ENABLED = os.environ.get("RUN_PDF_FILL_BENCHMARK") == "1"
ORG = os.environ.get("PDF_FILL_BENCHMARK_ORG")
BENCH = Path(__file__).resolve().parents[1] / "benchmark" / "pdf_fill"
ROOT = Path(__file__).resolve().parents[2]
ROUNDTRIP_FLOOR = 0.80  # below this is a flagged opportunity, not a hard failure (see README)

pytestmark = pytest.mark.skipif(
    not (ENABLED and ORG),
    reason="set RUN_PDF_FILL_BENCHMARK=1 and PDF_FILL_BENCHMARK_ORG=o_... to run the real-LLM benchmark",
)


def _ensure_corpus() -> None:
    subprocess.run(["uv", "run", "python", str(BENCH / "build_corpus.py")], cwd=ROOT, check=True, timeout=600)


def test_curated_cases_pass() -> None:
    assert ORG is not None  # guaranteed by pytestmark skip
    _ensure_corpus()
    proc = subprocess.run(
        ["uv", "run", "python", str(BENCH / "run_benchmark.py"), "--org-id", ORG],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    print(proc.stdout[-2000:])
    assert proc.returncode == 0, "a curated case regressed (must-pass gate)"


def test_roundtrip_accuracy_above_floor() -> None:
    assert ORG is not None  # guaranteed by pytestmark skip
    _ensure_corpus()
    proc = subprocess.run(
        ["uv", "run", "python", str(BENCH / "run_roundtrip.py"), "--org-id", ORG, "--glob", "irs_w9.pdf"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    print(proc.stdout[-2000:])
    m = re.search(r"^TOTAL\s+\d+\s+(\d+)%", proc.stdout, re.MULTILINE)
    assert m, f"could not parse round-trip accuracy:\n{proc.stdout[-1000:]}"
    accuracy = int(m.group(1)) / 100
    assert accuracy >= ROUNDTRIP_FLOOR, f"round-trip accuracy {accuracy:.0%} < floor {ROUNDTRIP_FLOOR:.0%}"
