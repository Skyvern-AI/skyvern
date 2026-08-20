"""An iframe's contents must reach the NESTED tree, not just the flat element list.

`buildTreeFromBody` returns the same dict objects in both the flat `elements` list and the nested
`element_tree`, and the scraper used to attach a frame's subtree by writing only to the flat list --
correct only because Playwright's deserializer revives repeated objects by reference, so the two
structures aliased and one write reached both.

A raw-CDP engine returns evaluate results with `returnByValue`, a JSON round-trip that copies repeated
objects instead of aliasing them. The flat write then left the tree's iframe node with `children: []`,
so the model was shown `<iframe></iframe>` and invented element ids for the fields it could not see.
Every other artifact was correct -- `elements`, `id_to_element_dict`, `id_to_css_dict`,
`id_to_frame_dict` -- which is why nothing caught it for the entire life of the engine branch.

These tests use non-aliased structures on purpose: that is the shape an engine without reference
revival produces, and asserting on it keeps the scraper from silently depending on aliasing again.
"""

from __future__ import annotations

from skyvern.webeye.scraper.scraper import _attach_frame_subtree


def _subtree() -> list[dict]:
    return [{"id": "BAAD", "tagName": "input", "attributes": {"name": "company"}, "children": []}]


def test_the_iframe_node_gets_its_frames_children() -> None:
    tree = [{"id": "AAAD", "tagName": "iframe", "children": []}]
    assert _attach_frame_subtree(tree, "AAAD", _subtree()) is True
    assert tree[0]["children"][0]["id"] == "BAAD"


def test_it_finds_an_iframe_nested_below_the_top_level() -> None:
    """Real pages wrap iframes in layout elements, so a top-level-only scan would miss most of them."""
    tree = [
        {
            "id": "AAAB",
            "tagName": "div",
            "children": [
                {
                    "id": "AAAC",
                    "tagName": "section",
                    "children": [
                        {"id": "AAAD", "tagName": "iframe", "children": []},
                    ],
                }
            ],
        },
    ]
    assert _attach_frame_subtree(tree, "AAAD", _subtree()) is True
    assert tree[0]["children"][0]["children"][0]["children"][0]["attributes"]["name"] == "company"


def test_a_missing_node_is_reported_rather_than_silently_ignored() -> None:
    """The caller logs on False; returning True here would restore the silent failure."""
    tree = [{"id": "AAAB", "tagName": "div", "children": []}]
    assert _attach_frame_subtree(tree, "AAAD", _subtree()) is False


def test_the_flat_list_and_the_tree_both_end_up_populated_when_they_do_not_alias() -> None:
    """The regression in one assertion.

    Two dicts with the same id and no shared identity -- exactly what a by-value engine returns. The
    old code wrote only to the flat entry, so the tree node stayed empty while every flat-list
    assertion still passed.
    """
    flat_iframe = {"id": "AAAD", "tagName": "iframe", "children": []}
    tree_iframe = {"id": "AAAD", "tagName": "iframe", "children": []}
    assert flat_iframe is not tree_iframe

    subtree = _subtree()
    _attach_frame_subtree([tree_iframe], "AAAD", subtree)
    for element in [flat_iframe]:
        if element["id"] == "AAAD":
            element["children"] = subtree

    assert flat_iframe["children"], "the flat list lost the frame's elements"
    assert tree_iframe["children"], "the nested tree lost the frame's elements -- this is the bug"


def test_an_aliasing_engine_is_unaffected() -> None:
    """Under Playwright the two writes land on one dict; assigning twice must stay harmless."""
    shared = {"id": "AAAD", "tagName": "iframe", "children": []}
    subtree = _subtree()
    _attach_frame_subtree([shared], "AAAD", subtree)
    shared["children"] = subtree
    assert shared["children"] == subtree
