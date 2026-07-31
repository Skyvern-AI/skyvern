"""Identical SVG icons convert once; duplicates resolve from the warmed cache (no svg-convert flood).

These pin the dedup partitioning (the part that prevents N identical icons from each firing their own
LLM call). The cache-key derivation itself is verbatim existing code, so it is exercised via an injected
key function here rather than standing up a SkyvernContext.
"""

from __future__ import annotations

from skyvern.forge.agent_functions import _partition_svgs_by_cache_key


def _key(element: dict) -> str:
    return element["k"]


def test_identical_keys_collapse_to_one_representative() -> None:
    elements = [{"k": "a", "id": 1}, {"k": "a", "id": 2}, {"k": "a", "id": 3}]
    reps, dups = _partition_svgs_by_cache_key(elements, key_fn=_key)
    assert reps == [{"k": "a", "id": 1}]
    assert dups == [{"k": "a", "id": 2}, {"k": "a", "id": 3}]


def test_distinct_keys_are_all_representatives() -> None:
    elements = [{"k": "a"}, {"k": "b"}, {"k": "c"}]
    reps, dups = _partition_svgs_by_cache_key(elements, key_fn=_key)
    assert reps == elements
    assert dups == []


def test_mixed_keys_dedup_per_group_and_account_for_every_element() -> None:
    elements = [
        {"k": "a", "id": 1},
        {"k": "b", "id": 2},
        {"k": "a", "id": 3},
        {"k": "b", "id": 4},
        {"k": "c", "id": 5},
    ]
    reps, dups = _partition_svgs_by_cache_key(elements, key_fn=_key)
    assert reps == [{"k": "a", "id": 1}, {"k": "b", "id": 2}, {"k": "c", "id": 5}]
    assert dups == [{"k": "a", "id": 3}, {"k": "b", "id": 4}]
    assert len(reps) + len(dups) == len(elements)


def test_key_fn_failure_fails_open_to_representative() -> None:
    def boom_on_bad(element: dict) -> str:
        if element.get("bad"):
            raise ValueError("no key")
        return element["k"]

    elements = [{"k": "a"}, {"bad": True}, {"k": "a"}]
    reps, dups = _partition_svgs_by_cache_key(elements, key_fn=boom_on_bad)
    assert reps == [{"k": "a"}, {"bad": True}]
    assert dups == [{"k": "a"}]
    assert len(reps) + len(dups) == len(elements)


def test_empty_input() -> None:
    reps, dups = _partition_svgs_by_cache_key([], key_fn=_key)
    assert reps == []
    assert dups == []
