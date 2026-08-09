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


def test_trim_on_fast_copy_leaves_source_intact() -> None:
    tree = _sample_tree()
    trim_element_tree(_deepcopy_element_tree(tree))

    assert tree[0]["frame_index"] == 0
    assert tree[0]["id"] == "0"
    assert tree[0]["rect"] == {"x": 1, "y": 2, "width": 3.5, "height": 4.5}
    assert tree[0]["children"][0]["frame"] == "child.frame"
    assert tree[0]["children"][0]["frame_index"] == 1
