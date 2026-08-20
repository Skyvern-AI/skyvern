"""The scraper's three views of one page must agree with each other.

A scrape produces the same elements three times: the flat ``elements`` list, the ``id_to_*`` lookup
dicts, and the nested ``element_tree``. The model is shown only the tree; everything else drives
action resolution. Nothing compared them, and that is exactly how an iframe's contents went missing
for the entire life of the raw-CDP engine branch -- the flat list and every dict were correct while
the tree's iframe node was empty, so every assertion in the suite passed and the model was handed
``<iframe></iframe>``.

These checks are engine-agnostic on purpose. The bug was not in an engine; it was the scraper
depending on evaluate() returning aliased objects, which one driver happens to provide and another
does not. Anything that changes how evaluate results are deserialized can break the same way, so the
invariant belongs to the scraper rather than to a driver's test suite.

`check_element_tree_consistency` is importable: the cross-engine browser test asserts it after every
real scrape, where it is worth far more than it is here on synthetic input.
"""

from __future__ import annotations

from typing import Any


def _walk(nodes: list[dict], seen: list[str]) -> None:
    for node in nodes or []:
        node_id = node.get("id")
        if node_id is not None:
            seen.append(str(node_id))
        _walk(node.get("children") or [], seen)


def check_element_tree_consistency(
    elements: list[dict],
    element_tree: list[dict],
    id_to_element_dict: dict[str, Any] | None = None,
) -> list[str]:
    """Return one message per violation; empty means the three views agree."""
    tree_ids: list[str] = []
    _walk(element_tree, tree_ids)
    tree_id_set = set(tree_ids)
    flat_ids = {str(element["id"]) for element in elements if element.get("id") is not None}

    problems: list[str] = []

    missing_from_tree = flat_ids - tree_id_set
    if missing_from_tree:
        # The iframe bug's signature: present in the flat list, absent from what the model reads.
        problems.append(
            f"{len(missing_from_tree)} element(s) in the flat list are unreachable in the tree the "
            f"model is shown: {sorted(missing_from_tree)[:8]}"
        )

    if id_to_element_dict:
        missing_from_dict = {str(k) for k in id_to_element_dict} - tree_id_set
        if missing_from_dict:
            problems.append(
                f"{len(missing_from_dict)} id(s) in id_to_element_dict are unreachable in the tree: "
                f"{sorted(missing_from_dict)[:8]}"
            )

    duplicated = {node_id for node_id in tree_id_set if tree_ids.count(node_id) > 1}
    if duplicated:
        # Two parents claiming one element makes action resolution order-dependent.
        problems.append(f"{len(duplicated)} id(s) appear more than once in the tree: {sorted(duplicated)[:8]}")

    return problems


def _node(node_id: str, children: list[dict] | None = None) -> dict:
    return {"id": node_id, "tagName": "div", "children": children or []}


def test_agreeing_views_report_nothing() -> None:
    tree = [_node("A", [_node("B")])]
    elements = [{"id": "A"}, {"id": "B"}]
    assert check_element_tree_consistency(elements, tree, {"A": {}, "B": {}}) == []


def test_an_element_missing_from_the_tree_is_reported() -> None:
    """The iframe regression in miniature: the flat list has it, the tree does not."""
    tree = [_node("AAAD")]
    elements = [{"id": "AAAD"}, {"id": "BAAD"}]
    problems = check_element_tree_consistency(elements, tree)
    assert len(problems) == 1
    assert "BAAD" in problems[0]
    assert "model is shown" in problems[0]


def test_an_empty_iframe_node_is_caught_even_though_every_other_view_is_correct() -> None:
    """The exact shape that shipped: the dicts and the flat list agree, only the tree is truncated."""
    tree = [_node("AAAD")]  # the iframe, with children: []
    elements = [{"id": "AAAD"}, {"id": "BAAC"}, {"id": "BAAD"}, {"id": "BAAG"}]
    id_to_element = {"AAAD": {}, "BAAC": {}, "BAAD": {}, "BAAG": {}}
    problems = check_element_tree_consistency(elements, tree, id_to_element)
    assert len(problems) == 2, problems
    assert any("flat list" in p for p in problems)
    assert any("id_to_element_dict" in p for p in problems)


def test_a_duplicated_id_is_reported() -> None:
    shared = _node("DUP")
    tree = [_node("A", [shared]), _node("B", [shared])]
    problems = check_element_tree_consistency([{"id": "A"}, {"id": "B"}, {"id": "DUP"}], tree)
    assert any("more than once" in p for p in problems)


def test_deeply_nested_elements_count_as_reachable() -> None:
    """A frame subtree attaches several levels down; a shallow walk would report false violations."""
    tree = [_node("A", [_node("B", [_node("C", [_node("D")])])])]
    elements = [{"id": i} for i in ("A", "B", "C", "D")]
    assert check_element_tree_consistency(elements, tree) == []


def test_extra_nodes_in_the_tree_are_not_a_violation() -> None:
    """Non-interactable nodes legitimately appear in the tree without being in the flat list."""
    tree = [_node("A", [_node("layout-only")])]
    assert check_element_tree_consistency([{"id": "A"}], tree) == []
