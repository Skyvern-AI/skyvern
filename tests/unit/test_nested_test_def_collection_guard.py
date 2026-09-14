"""A `test_*` def nested inside a helper function is never collected, so a deleted class header can
silently retire a whole block of tests (SKY-16226). A test nested inside another test is a local fake."""

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]


def _test_defs_nested_in_helpers(tree: ast.Module) -> list[tuple[int, str, str]]:
    offenders: list[tuple[int, str, str]] = []

    def walk(node: ast.AST, enclosing_helper: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            helper = enclosing_helper
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_") and enclosing_helper is not None:
                    offenders.append((child.lineno, enclosing_helper, child.name))
                if not child.name.startswith("test_"):
                    helper = helper or child.name
            walk(child, helper)

    walk(tree, None)
    return offenders


def test_no_test_def_is_nested_inside_a_helper_function() -> None:
    offenders: list[str] = []
    for pattern in ("test_*.py", "*_test.py"):
        for path in sorted(TESTS_ROOT.rglob(pattern)):
            tree = ast.parse(path.read_text(), filename=str(path))
            for lineno, helper, name in _test_defs_nested_in_helpers(tree):
                offenders.append(f"{path.relative_to(TESTS_ROOT.parent)}:{lineno} {name} is nested inside {helper}")
    assert offenders == [], "\n".join(offenders)
