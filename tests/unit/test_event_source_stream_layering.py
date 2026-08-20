"""Guard: the SSE transport is imported from core/, never routes/.

`EventSourceStream` / `FastAPIEventSourceStream` are transport-agnostic infra and
live in `skyvern/forge/sdk/core/event_source_stream.py` (SKY-11694). They used to
live under `routes/`, which forced copilot domain modules into copilot→routes
layering inversions. This guard fails if anything re-imports them from the old
routes path.
"""

from __future__ import annotations

import ast
from pathlib import Path

SKYVERN_ROOT = Path(__file__).resolve().parents[2] / "skyvern"
OLD_MODULE = "skyvern.forge.sdk.routes.event_source_stream"


def _imports_old_path(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == OLD_MODULE:
            return True
        if isinstance(node, ast.Import) and any(alias.name == OLD_MODULE for alias in node.names):
            return True
    return False


def test_no_imports_of_event_source_stream_from_routes() -> None:
    offenders = [
        str(path.relative_to(SKYVERN_ROOT)) for path in sorted(SKYVERN_ROOT.rglob("*.py")) if _imports_old_path(path)
    ]
    assert offenders == [], f"event_source_stream must be imported from core/, not routes/: {offenders}"
