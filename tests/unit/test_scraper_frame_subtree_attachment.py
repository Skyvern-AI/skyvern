"""An iframe's contents must reach the nested element tree, which a by-value CDP engine does not alias."""

from __future__ import annotations

from typing import Any

from skyvern.webeye.scraper.scraper import _attach_frame_subtree


def _subtree() -> list[dict]:
    return [{"id": "BAAD", "tagName": "input", "attributes": {"name": "company"}, "children": []}]


def test_an_iframe_nested_below_the_top_level_gets_its_frames_children() -> None:
    tree: list[dict[str, Any]] = [
        {
            "id": "AAAB",
            "tagName": "div",
            "children": [
                {"id": "AAAC", "tagName": "section", "children": [{"id": "AAAD", "tagName": "iframe", "children": []}]}
            ],
        },
    ]
    assert _attach_frame_subtree(tree, "AAAD", _subtree()) is True
    assert tree[0]["children"][0]["children"][0]["children"][0]["attributes"]["name"] == "company"


def test_a_missing_iframe_node_is_reported_rather_than_silently_ignored() -> None:
    tree = [{"id": "AAAB", "tagName": "div", "children": []}]
    assert _attach_frame_subtree(tree, "AAAD", _subtree()) is False
