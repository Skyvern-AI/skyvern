"""Guard against import-order-dependent circular imports in the generated SDK types.

Fern emits mutually recursive union modules that cross-import each other at module
scope. Whether that explodes depends on which module the interpreter reaches first,
so an in-process import check cannot see it — each module needs a fresh interpreter.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import skyvern.client.types as client_types

_TYPES_DIR = Path(client_types.__file__).parent
_PACKAGE = client_types.__name__


def _cross_imports(path: Path) -> set[str]:
    """Sibling modules imported at module scope by `path`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    siblings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            if (_TYPES_DIR / f"{node.module}.py").exists():
                siblings.add(node.module)
    return siblings


def _modules_in_cycles() -> list[str]:
    graph = {p.stem: _cross_imports(p) for p in _TYPES_DIR.glob("*.py") if p.stem != "__init__"}
    cyclic: set[str] = set()
    for start in graph:
        seen = {start}
        queue = list(graph[start])
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(graph.get(node, ()))
        if start in {n for node in seen for n in graph.get(node, ())}:
            cyclic.add(start)
    return sorted(cyclic)


def test_cyclic_type_modules_import_as_entry_point() -> None:
    """Every module in an import cycle must import cleanly as the interpreter's entry point."""
    modules = _modules_in_cycles()
    assert modules, "No cyclic generated type modules were discovered — the guard would be vacuous."

    failures: dict[str, str] = {}
    for module in modules:
        result = subprocess.run(
            [sys.executable, "-c", f"import {_PACKAGE}.{module}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures[module] = result.stderr.strip().splitlines()[-1]

    assert not failures, f"Generated type modules that fail when imported first: {failures}"
