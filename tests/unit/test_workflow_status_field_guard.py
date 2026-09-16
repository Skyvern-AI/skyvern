"""AST guard against reintroducing the ``workflow_status`` log field.

``workflow_status`` was a redundant duplicate of ``workflow_run_status``, emitted only by the
``mark_workflow_run_as_*`` wrappers. The unified finalizer ``_finalize_workflow_run_status``
bypasses those wrappers for completed/terminated runs while failures still route through
``mark_workflow_run_as_failed``, so the field was populated on some terminal paths and not
others -- a biased sample that reads as a ~99% failure rate (measured 7 completed vs 1,882
failed over 3h, against a true 4,548 / 949).

``workflow_run_status`` on the ``"Workflow run duration metrics"`` log is the canonical outcome
facet: ``_after_workflow_run_status_write`` emits exactly one per final status write, and both
status-write helpers call it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED_ROOTS = ("skyvern", "cloud")
_BANNED_KEYWORD = "workflow_status"


def _log_call_keywords(tree: ast.AST) -> list[tuple[int, str]]:
    """Keyword names passed to ``LOG.<method>(...)`` calls, with line numbers."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        receiver = func.value
        if not (isinstance(receiver, ast.Name) and receiver.id == "LOG"):
            continue
        for keyword in node.keywords:
            if keyword.arg is not None:
                found.append((node.lineno, keyword.arg))
    return found


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCANNED_ROOTS:
        files.extend((_REPO_ROOT / root).rglob("*.py"))
    return files


def test_no_production_log_emits_workflow_status() -> None:
    offenders: list[str] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our source
            continue
        for lineno, keyword in _log_call_keywords(tree):
            if keyword == _BANNED_KEYWORD:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")

    assert not offenders, (
        "`workflow_status` is a biased half-sample of terminal outcomes -- the unified finalizer "
        "bypasses the mark_* wrappers that emit it. Use `workflow_run_status` on the "
        '"Workflow run duration metrics" log instead. Offenders: ' + ", ".join(sorted(offenders))
    )


def test_guard_detects_a_planted_violation() -> None:
    """The guard must fail on a real violation, not merely pass on clean source."""
    tree = ast.parse('LOG.info("x", workflow_run_id="wr_1", workflow_status="completed")\n')
    assert (1, _BANNED_KEYWORD) in _log_call_keywords(tree)
