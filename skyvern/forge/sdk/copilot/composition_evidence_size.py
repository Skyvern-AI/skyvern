"""Shared size-compaction facts for bounded composition evidence."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def size_compaction_omits(evidence: dict[str, Any], categories: Iterable[str]) -> bool:
    compaction = evidence.get("size_compaction")
    if not isinstance(compaction, dict):
        return False
    category_set = set(categories)
    return any(
        isinstance(omission, dict) and omission.get("category") in category_set
        for omission in compaction.get("omissions") or []
    )
