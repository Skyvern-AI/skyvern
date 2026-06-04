"""Tests for animation wait span attribute instrumentation."""

import ast
import inspect
import os

from skyvern.webeye.utils.page import SkyvernFrame


def test_safe_wait_caller_defaults_to_unknown() -> None:
    sig = inspect.signature(SkyvernFrame.safe_wait_for_animation_end)
    assert "caller" in sig.parameters
    assert sig.parameters["caller"].default == "unknown"


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
                        tree = ast.parse(f.read(), filename=fpath)
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
