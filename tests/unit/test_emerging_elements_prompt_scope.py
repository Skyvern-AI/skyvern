"""Tests for SKY-9948: select_from_emerging_elements prompt scope reduction.

Verifies that the custom-select prompt for emerging elements contains only
new-element subtrees instead of the full page DOM.
"""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.actions.handler import _extract_new_subtrees
from skyvern.webeye.scraper.scraped_page import json_to_html


@pytest.fixture(autouse=True)
def _scoped_context():
    """`json_to_html` calls `skyvern_context.ensure_context()`, so we need one."""
    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        yield


class TestExtractNewSubtrees:
    """Core logic: walk the tree and return minimal subtrees rooted at new IDs."""

    def test_direct_root_match(self) -> None:
        tree = [{"id": "NEW", "tagName": "div", "children": []}]
        result = _extract_new_subtrees(tree, {"NEW"})
        assert len(result) == 1
        assert result[0]["id"] == "NEW"

    def test_no_match_returns_empty(self) -> None:
        tree = [{"id": "OLD", "tagName": "div", "children": []}]
        assert _extract_new_subtrees(tree, {"NEW"}) == []

    def test_empty_target_ids(self) -> None:
        tree = [{"id": "A", "tagName": "div", "children": []}]
        assert _extract_new_subtrees(tree, set()) == []

    def test_child_match_skips_parent(self) -> None:
        """A new element inside an old container: extract only the new child, not the parent."""
        tree = [
            {
                "id": "OLD_CONTAINER",
                "tagName": "div",
                "children": [
                    {"id": "NEW_ITEM", "tagName": "span", "children": []},
                ],
            },
        ]
        result = _extract_new_subtrees(tree, {"NEW_ITEM"})
        assert len(result) == 1
        assert result[0]["id"] == "NEW_ITEM"

    def test_new_parent_includes_new_children(self) -> None:
        """A new parent with new children: include the parent (children come along via nesting)."""
        tree = [
            {
                "id": "OLD",
                "tagName": "div",
                "children": [
                    {
                        "id": "NEW_PARENT",
                        "tagName": "ul",
                        "children": [
                            {"id": "NEW_CHILD1", "tagName": "li", "children": []},
                            {"id": "NEW_CHILD2", "tagName": "li", "children": []},
                        ],
                    },
                ],
            },
        ]
        result = _extract_new_subtrees(tree, {"NEW_PARENT", "NEW_CHILD1", "NEW_CHILD2"})
        assert len(result) == 1
        assert result[0]["id"] == "NEW_PARENT"
        assert len(result[0]["children"]) == 2

    def test_deep_nesting_skips_all_old_ancestors(self) -> None:
        tree = [
            {
                "id": "L0",
                "tagName": "div",
                "children": [
                    {
                        "id": "L1",
                        "tagName": "div",
                        "children": [
                            {
                                "id": "L2",
                                "tagName": "div",
                                "children": [
                                    {"id": "LEAF_NEW", "tagName": "span", "children": []},
                                ],
                            },
                        ],
                    },
                ],
            },
        ]
        result = _extract_new_subtrees(tree, {"LEAF_NEW"})
        assert len(result) == 1
        assert result[0]["id"] == "LEAF_NEW"

    def test_multiple_new_siblings(self) -> None:
        """Multiple new items at the same level inside an old parent."""
        tree = [
            {
                "id": "OLD_MENU",
                "tagName": "ul",
                "children": [
                    {"id": "OLD_ITEM", "tagName": "li", "children": [], "text": "Save"},
                    {"id": "NEW_A", "tagName": "li", "children": [], "text": "Export"},
                    {"id": "NEW_B", "tagName": "li", "children": [], "text": "Print"},
                ],
            },
        ]
        result = _extract_new_subtrees(tree, {"NEW_A", "NEW_B"})
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {"NEW_A", "NEW_B"}

    def test_element_without_id(self) -> None:
        tree = [{"tagName": "div", "children": [{"id": "NEW", "tagName": "span", "children": []}]}]
        result = _extract_new_subtrees(tree, {"NEW"})
        assert len(result) == 1
        assert result[0]["id"] == "NEW"

    def test_element_without_children_key(self) -> None:
        tree = [{"id": "NEW", "tagName": "div"}]
        result = _extract_new_subtrees(tree, {"NEW"})
        assert len(result) == 1


class TestPortalVsNonPortalScenarios:
    """End-to-end scenarios matching real page structures."""

    @pytest.fixture
    def portal_page(self) -> list[dict]:
        """Page with a large table + a Quasar portal dropdown appended to <body>."""
        return [
            {
                "id": "AAA",
                "tagName": "div",
                "attributes": {"class": "report-container"},
                "children": [
                    {
                        "id": "AAB",
                        "tagName": "table",
                        "attributes": {},
                        "children": [
                            {"id": "AAC", "tagName": "tr", "attributes": {}, "children": [], "text": "row1"},
                            {"id": "AAD", "tagName": "tr", "attributes": {}, "children": [], "text": "row2"},
                        ],
                        "text": "",
                    },
                ],
                "text": "",
            },
            {
                "id": "BBB",
                "tagName": "div",
                "attributes": {"class": "q-menu"},
                "children": [
                    {
                        "id": "BBC",
                        "tagName": "div",
                        "attributes": {"class": "q-item"},
                        "interactable": True,
                        "children": [],
                        "text": "Export to CSV",
                    },
                    {
                        "id": "BBD",
                        "tagName": "div",
                        "attributes": {"class": "q-item"},
                        "interactable": True,
                        "children": [],
                        "text": "Data Replace",
                    },
                ],
                "text": "",
            },
        ]

    @pytest.fixture
    def non_portal_page(self) -> list[dict]:
        """Page where dropdown is injected inside the existing app container (not a portal)."""
        return [
            {
                "id": "APP",
                "tagName": "div",
                "attributes": {"class": "app"},
                "children": [
                    {
                        "id": "TABLE",
                        "tagName": "table",
                        "attributes": {},
                        "children": [
                            {"id": "ROW1", "tagName": "tr", "attributes": {}, "children": [], "text": "row1"},
                            {"id": "ROW2", "tagName": "tr", "attributes": {}, "children": [], "text": "row2"},
                        ],
                        "text": "",
                    },
                    {
                        "id": "TOOLBAR",
                        "tagName": "div",
                        "attributes": {"class": "toolbar"},
                        "children": [
                            {"id": "BTN", "tagName": "button", "attributes": {}, "children": [], "text": "Actions"},
                            {
                                "id": "DROPDOWN",
                                "tagName": "div",
                                "attributes": {"class": "dropdown-menu"},
                                "children": [
                                    {
                                        "id": "OPT1",
                                        "tagName": "div",
                                        "attributes": {},
                                        "interactable": True,
                                        "children": [],
                                        "text": "Export to CSV",
                                    },
                                    {
                                        "id": "OPT2",
                                        "tagName": "div",
                                        "attributes": {},
                                        "interactable": True,
                                        "children": [],
                                        "text": "Print",
                                    },
                                ],
                                "text": "",
                            },
                        ],
                        "text": "",
                    },
                ],
                "text": "",
            },
        ]

    # --- Portal scenario ---

    def test_portal_extracts_only_dropdown(self, portal_page: list[dict]) -> None:
        new_ids = {"BBB", "BBC", "BBD"}
        result = _extract_new_subtrees(portal_page, new_ids)
        assert len(result) == 1
        assert result[0]["id"] == "BBB"

    def test_portal_preserves_hierarchy(self, portal_page: list[dict]) -> None:
        new_ids = {"BBB", "BBC", "BBD"}
        result = _extract_new_subtrees(portal_page, new_ids)
        children_ids = [c["id"] for c in result[0]["children"]]
        assert children_ids == ["BBC", "BBD"]

    def test_portal_excludes_table(self, portal_page: list[dict]) -> None:
        new_ids = {"BBB", "BBC", "BBD"}
        result = _extract_new_subtrees(portal_page, new_ids)
        all_ids = {r["id"] for r in result}
        assert "AAA" not in all_ids
        assert "AAB" not in all_ids

    def test_portal_html_contains_menu_text(self, portal_page: list[dict]) -> None:
        new_ids = {"BBB", "BBC", "BBD"}
        result = _extract_new_subtrees(portal_page, new_ids)
        html = "".join(json_to_html(el, need_skyvern_attrs=False) for el in result)
        assert "Export to CSV" in html
        assert "Data Replace" in html
        assert "row1" not in html

    # --- Non-portal scenario (Concern 2 & 3) ---

    def test_non_portal_extracts_dropdown_not_whole_page(self, non_portal_page: list[dict]) -> None:
        """Key test: new elements inside existing container should NOT include the whole page."""
        new_ids = {"DROPDOWN", "OPT1", "OPT2"}
        result = _extract_new_subtrees(non_portal_page, new_ids)
        assert len(result) == 1
        assert result[0]["id"] == "DROPDOWN"

    def test_non_portal_excludes_table_rows(self, non_portal_page: list[dict]) -> None:
        new_ids = {"DROPDOWN", "OPT1", "OPT2"}
        result = _extract_new_subtrees(non_portal_page, new_ids)
        html = "".join(json_to_html(el, need_skyvern_attrs=False) for el in result)
        assert "row1" not in html
        assert "row2" not in html

    def test_non_portal_preserves_dropdown_children(self, non_portal_page: list[dict]) -> None:
        new_ids = {"DROPDOWN", "OPT1", "OPT2"}
        result = _extract_new_subtrees(non_portal_page, new_ids)
        html = "".join(json_to_html(el, need_skyvern_attrs=False) for el in result)
        assert "Export to CSV" in html
        assert "Print" in html

    def test_non_portal_leaf_only_new_ids(self, non_portal_page: list[dict]) -> None:
        """Only leaf items are new (parent DROPDOWN is old) — extract each leaf separately."""
        new_ids = {"OPT1", "OPT2"}
        result = _extract_new_subtrees(non_portal_page, new_ids)
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {"OPT1", "OPT2"}

    # --- Mixed / edge cases ---

    def test_mixed_portal_and_inline_new(self, portal_page: list[dict]) -> None:
        """New element injected inside old container AND a portal — both are extracted."""
        portal_page[0]["children"].append(
            {"id": "INLINE_NEW", "tagName": "span", "attributes": {}, "children": [], "text": "injected"}
        )
        new_ids = {"BBB", "BBC", "BBD", "INLINE_NEW"}
        result = _extract_new_subtrees(portal_page, new_ids)
        ids = {r["id"] for r in result}
        assert ids == {"BBB", "INLINE_NEW"}

    def test_empty_tree(self) -> None:
        assert _extract_new_subtrees([], {"NEW"}) == []
