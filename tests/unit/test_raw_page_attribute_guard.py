"""AST guard: attributes accessed on ``_get_raw_page(...)`` must exist on the driver ``Page``.

``_get_raw_page`` returns the raw engine page, not the ``_SkyvernPageBase``
wrapper, so a wrapper-only method called on its result (``get_actual_value``,
``magic_link``, ...) raises ``AttributeError`` only at runtime: the pre-commit
mypy environment does not install a browser driver, resolves ``Page`` to
``Any``, and never flags the call. This guard checks the real driver API.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.async_api import Page

_PAGE_ATTRS = frozenset(dir(Page))


@dataclass(frozen=True)
class Violation:
    filename: str
    lineno: int
    attr: str


def find_raw_page_attribute_misuses(src: str, filename: str = "<src>") -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(ast.parse(src, filename=filename)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_get_raw_page"
            and node.attr not in _PAGE_ATTRS
        ):
            violations.append(Violation(filename, node.lineno, node.attr))
    return violations


def test_flags_wrapper_only_method_on_raw_page() -> None:
    src = "async def f(p):\n    return await _get_raw_page(p).get_actual_value('x')\n"
    assert [v.attr for v in find_raw_page_attribute_misuses(src)] == ["get_actual_value"]


def test_ignores_real_driver_page_attributes() -> None:
    src = "def f(p):\n    _get_raw_page(p).locator('a')\n    return _get_raw_page(p).url\n"
    assert find_raw_page_attribute_misuses(src) == []


def test_ignores_chained_attribute_off_a_driver_attribute() -> None:
    src = "async def f(p):\n    await _get_raw_page(p).keyboard.press('Enter')\n"
    assert find_raw_page_attribute_misuses(src) == []


# --- repo enforcement -------------------------------------------------------


def _production_python_files() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[2]
    files: list[Path] = []
    for package in ("skyvern", "cloud"):
        root = repo_root / package
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if "tests" in parts or path.name.startswith("test_") or "__pycache__" in parts:
                continue
            files.append(path)
    return files


def test_no_wrapper_attribute_access_on_raw_pages_in_production() -> None:
    offenders: list[Violation] = []
    for path in _production_python_files():
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            offenders.extend(find_raw_page_attribute_misuses(src, str(path)))
        except SyntaxError:
            continue
    assert not offenders, (
        "_get_raw_page(...) returns the raw driver Page; these attributes do not "
        "exist on it and raise AttributeError at runtime. Call them on the "
        "_SkyvernPageBase wrapper instead:\n"
        + "\n".join(f"  {o.filename}:{o.lineno} _get_raw_page(...).{o.attr}" for o in offenders)
    )


def test_repo_scan_finds_at_least_the_production_tree() -> None:
    # Guard against the enforcement test silently passing because the scan walked
    # nothing (wrong root, glob typo). skyvern/** must always yield files.
    assert len(_production_python_files()) > 100


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
