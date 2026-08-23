"""Structure-aware deep copy of the scraped element tree.

``_deepcopy_element_tree`` replaces two ``copy.deepcopy(element_tree)`` calls in
``scrape_web_unsafe``. The element tree is pure JSON-like data (acyclic, only
dict/list containers with immutable leaves, ``json.dumps``-serialized by
``hash_element``), so the fast copy must be byte-for-byte equivalent to
``copy.deepcopy`` while sharing no mutable container with the source.
"""

from __future__ import annotations

import copy

from skyvern.webeye.scraper.scraper import _deepcopy_element_tree, trim_element_tree


def _sample_tree() -> list[dict]:
    return [
        {
            "id": "0",
            "tagName": "form",
            "frame": "main.frame",
            "frame_index": 0,
            "rect": {"x": 1, "y": 2, "width": 3.5, "height": 4.5},
            "attributes": {"class": "a b", "name": "signup"},
            "interactable": False,
            "text": "",
            "children": [
                {
                    "id": "1",
                    "tagName": "input",
                    "frame": "child.frame",
                    "frame_index": 1,
                    "rect": {"x": 5, "y": 6, "width": 7, "height": 8},
                    "attributes": {"type": "text", "value": None},
                    "interactable": True,
                    "children": [],
                },
            ],
        },
    ]


def test_deepcopy_element_tree_matches_stdlib_deepcopy() -> None:
    tree = _sample_tree()
    assert _deepcopy_element_tree(tree) == copy.deepcopy(tree)


def test_deepcopy_element_tree_isolates_nested_containers() -> None:
    tree = _sample_tree()
    fast = _deepcopy_element_tree(tree)

    assert fast is not tree
    assert fast[0] is not tree[0]
    assert fast[0]["attributes"] is not tree[0]["attributes"]
    assert fast[0]["rect"] is not tree[0]["rect"]
    assert fast[0]["children"] is not tree[0]["children"]
    assert fast[0]["children"][0] is not tree[0]["children"][0]
    assert fast[0]["children"][0]["attributes"] is not tree[0]["children"][0]["attributes"]

    fast[0]["attributes"]["name"] = "MUTATED"
    fast[0]["children"][0]["rect"]["x"] = 999
    fast[0]["children"][0]["attributes"]["type"] = "MUTATED"
    assert tree[0]["attributes"]["name"] == "signup"
    assert tree[0]["children"][0]["rect"]["x"] == 5
    assert tree[0]["children"][0]["attributes"]["type"] == "text"


class _MutableLeaf:
    """A mutable, non-dict/list leaf that only ``copy.deepcopy`` can isolate."""

    def __init__(self, items: list[str]) -> None:
        self.items = items

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _MutableLeaf) and self.items == other.items


def test_deepcopy_element_tree_falls_back_to_deepcopy_for_non_json_leaf() -> None:
    leaf = _MutableLeaf(["shared"])
    tree = [{"id": "0", "custom": leaf}]

    fast = _deepcopy_element_tree(tree)
    copied = fast[0]["custom"]

    assert copied == leaf
    assert copied is not leaf
    assert copied.items is not leaf.items

    copied.items.append("MUTATED")
    assert leaf.items == ["shared"]


def test_deepcopy_element_tree_preserves_shared_container_identity() -> None:
    """A container referenced from two places must be copied once and aliased in the
    copy, exactly as ``copy.deepcopy`` does via its memo."""
    shared_rect = {"x": 1, "y": 2, "width": 3, "height": 4}
    tree = [
        {"id": "0", "rect": shared_rect},
        {"id": "1", "rect": shared_rect},
    ]

    fast = _deepcopy_element_tree(tree)

    assert fast == copy.deepcopy(tree)
    assert fast[0]["rect"] is fast[1]["rect"]
    assert fast[0]["rect"] is not shared_rect

    fast[0]["rect"]["x"] = 999
    assert fast[1]["rect"]["x"] == 999
    assert shared_rect["x"] == 1


def test_deepcopy_element_tree_handles_cyclic_graph() -> None:
    """A page-controlled cyclic dict/list graph must copy without ``RecursionError``
    and reproduce the cycle in the copy, matching ``copy.deepcopy`` semantics."""
    node: dict = {"id": "0", "children": []}
    node["self"] = node
    node["children"].append(node)
    tree = [node]

    fast = _deepcopy_element_tree(tree)

    copied = fast[0]
    assert copied is not node
    assert copied["self"] is copied
    assert copied["children"][0] is copied
    assert copied["id"] == "0"


def test_deepcopy_element_tree_preserves_outer_list_back_reference() -> None:
    """A back-reference to the outer list itself must be preserved exactly like
    ``copy.deepcopy``: the outer list has to enter the memoized path too."""
    tree: list[dict] = [{"id": "0"}]
    tree[0]["root"] = tree

    fast = _deepcopy_element_tree(tree)

    assert fast is not tree
    assert fast[0]["root"] is fast
    assert fast[0]["id"] == "0"


def test_trim_on_fast_copy_leaves_source_intact() -> None:
    tree = _sample_tree()
    trim_element_tree(_deepcopy_element_tree(tree))

    assert tree[0]["frame_index"] == 0
    assert tree[0]["id"] == "0"
    assert tree[0]["rect"] == {"x": 1, "y": 2, "width": 3.5, "height": 4.5}
    assert tree[0]["children"][0]["frame"] == "child.frame"
    assert tree[0]["children"][0]["frame_index"] == 1


def test_incremental_seam_double_copy_keeps_cleaned_trimmed_source_independent() -> None:
    """``IncrementalScrapePage.get_incremental_element_tree`` builds the cleaned and
    trimmed trees from two independent ``_deepcopy_element_tree`` copies of the raw
    JS tree. Both downstream trees are stored on the object and read by callers, so
    an in-place mutation of one must never leak into the other or into the source."""
    raw = _sample_tree()

    cleaned = _deepcopy_element_tree(raw)
    cleaned[0]["attributes"].pop("class")  # cleanup_element_tree mutates in place

    trimmed = trim_element_tree(_deepcopy_element_tree(cleaned))  # trim mutates in place

    assert raw[0]["attributes"] == {"class": "a b", "name": "signup"}
    assert "class" not in cleaned[0]["attributes"]
    cleaned_ids = {id(cleaned[0]), id(cleaned[0]["children"][0])}
    trimmed_ids = {id(trimmed[0]), id(trimmed[0]["children"][0])}
    assert cleaned_ids.isdisjoint(trimmed_ids)
    assert cleaned[0] is not raw[0]
