"""Guard: enforcement → streaming_adapter is a one-way, module-level dependency.

The two modules used to import each other lazily (a hidden cycle):
`streaming_adapter` reached into `enforcement` for the unrecoverable-tool-error
helpers, while `enforcement` lazily imported `stream_to_sse`. SKY-11695 extracts
that cluster into the `unrecoverable_tool_error` leaf module, so
`streaming_adapter` needs nothing from `enforcement`, and `enforcement` imports
`streaming_adapter` once at module level (calling it attribute-style so the
existing monkeypatch sites keep working). This guard fails if either direction
regresses.
"""

from __future__ import annotations

import ast
from pathlib import Path

COPILOT_DIR = Path(__file__).resolve().parents[2] / "skyvern" / "forge" / "sdk" / "copilot"


def _imports_matching(tree: ast.Module, suffix: str) -> list[ast.stmt]:
    matches: list[ast.stmt] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(suffix):
            matches.append(node)
        elif isinstance(node, ast.Import) and any(alias.name.endswith(suffix) for alias in node.names):
            matches.append(node)
    return matches


def test_enforcement_streaming_adapter_dependency_is_one_way() -> None:
    adapter_tree = ast.parse((COPILOT_DIR / "streaming_adapter.py").read_text(encoding="utf-8"))
    adapter_offenders = [node.lineno for node in _imports_matching(adapter_tree, "copilot.enforcement")]
    assert adapter_offenders == [], f"streaming_adapter imports enforcement at lines {adapter_offenders}"

    enforcement_tree = ast.parse((COPILOT_DIR / "enforcement.py").read_text(encoding="utf-8"))
    top_level = set(enforcement_tree.body)
    nested = [node.lineno for node in _imports_matching(enforcement_tree, "streaming_adapter") if node not in top_level]
    assert nested == [], f"enforcement imports streaming_adapter below module level at lines {nested}"
