"""
Benchmark and regression tests for DOM parser pipeline performance.

Tests the Python-side processing: trim_element_tree, _filter_attributes,
json_to_html, and cleanup traversal patterns.
"""

import copy
import time
from collections import deque

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.scraper.scraped_page import ScrapedPage, json_to_html
from skyvern.webeye.scraper.scraper import (
    _filter_attributes,
    _trimmed_attributes,
    _trimmed_base64_data,
    build_element_dict,
    trim_element,
    trim_element_tree,
)


@pytest.fixture(autouse=True)
def _setup_skyvern_context():
    """Ensure a SkyvernContext exists for tests that call json_to_html."""
    ctx = SkyvernContext()
    skyvern_context.set(ctx)
    yield
    skyvern_context.reset()


def _make_element(
    element_id: str,
    tag: str = "div",
    interactable: bool = True,
    text: str = "sample text",
    num_attrs: int = 10,
    children: list | None = None,
) -> dict:
    """Generate a realistic element dict for testing."""
    attrs = {
        "class": f"cls-{element_id}",
        "id": f"html-id-{element_id}",
        "data-testid": f"test-{element_id}",
        "style": "display: flex; align-items: center;",
        "aria-label": f"Element {element_id}",
        "role": "button",
        "type": "button",
        "name": f"name-{element_id}",
        "value": f"val-{element_id}",
        "placeholder": "Enter value...",
    }
    # Add extra non-reserved attributes to test filtering
    for i in range(max(0, num_attrs - len(attrs))):
        attrs[f"data-extra-{i}"] = f"extra-value-{i}"

    return {
        "id": element_id,
        "tagName": tag,
        "interactable": interactable,
        "text": text,
        "attributes": attrs,
        "children": children or [],
        "frame": "main.frame",
        "frame_index": 0,
        "keepAllAttr": False,
        "beforePseudoText": "",
        "afterPseudoText": "",
        "purgeable": False,
        "rect": {"top": 0, "left": 0, "bottom": 100, "right": 200, "width": 200, "height": 100},
    }


def _make_element_tree(num_elements: int, depth: int = 3) -> list[dict]:
    """Generate a tree of elements for benchmarking."""
    element_counter = 0
    children_per_node = max(1, num_elements // (depth + 1))

    def build_level(current_depth: int, remaining: int) -> list[dict]:
        nonlocal element_counter
        level_elements = []
        while remaining > 0 and element_counter < num_elements:
            element_counter += 1
            children = []
            if current_depth < depth and remaining > 1:
                child_count = min(children_per_node, remaining - 1)
                children = build_level(current_depth + 1, child_count)
                remaining -= len(children)
            el = _make_element(
                element_id=f"el_{element_counter:04d}",
                tag=["div", "span", "button", "input", "a"][element_counter % 5],
                interactable=element_counter % 3 == 0,
                text=f"Text content for element {element_counter}" if element_counter % 2 == 0 else "",
                children=children,
            )
            level_elements.append(el)
            remaining -= 1
        return level_elements

    return build_level(0, num_elements)


class TestFilterAttributesMerged:
    """Test that the new merged _filter_attributes produces the same output as the two-pass approach."""

    def test_basic_whitelist(self):
        attrs = {"class": "foo", "aria-label": "bar", "name": "baz", "data-x": "y"}
        result = _filter_attributes(attrs, keep_all=False)
        assert "aria-label" in result
        assert "name" in result
        assert "class" not in result
        assert "data-x" not in result

    def test_base64_removal(self):
        attrs = {"href": "data:image/png;base64,abc123", "name": "test", "src": "data:text/html;base64,xyz"}
        result = _filter_attributes(attrs, keep_all=False)
        assert "href" not in result
        assert "src" not in result
        assert result["name"] == "test"

    def test_keep_all_attr(self):
        attrs = {"class": "foo", "aria-label": "bar", "data-x": "y"}
        result = _filter_attributes(attrs, keep_all=True)
        # keepAllAttr=True should keep everything except base64
        assert "class" in result
        assert "data-x" in result

    def test_role_listbox_option(self):
        attrs = {"role": "listbox", "class": "foo"}
        result = _filter_attributes(attrs, keep_all=False)
        assert result["role"] == "listbox"
        assert "class" not in result

    def test_name_truncation_in_filter(self):
        """Name truncation should happen inside _filter_attributes (single pass)."""
        attrs = {"name": "x" * 1000, "aria-label": "test"}
        result = _filter_attributes(attrs, keep_all=False)
        assert len(result["name"]) == 500
        assert result["aria-label"] == "test"

    def test_equivalence_with_old_approach(self):
        """The new _filter_attributes should produce the same result as the old two-pass approach."""
        attrs = {
            "class": "foo",
            "aria-label": "bar",
            "name": "baz",
            "href": "data:image/png;base64,abc",
            "src": "https://example.com/image.png",
            "role": "option",
            "data-x": "y",
            "type": "button",
        }
        # Old approach: two passes
        old_pass1 = _trimmed_base64_data(attrs)
        old_result = _trimmed_attributes(old_pass1)

        # New approach: single pass
        new_result = _filter_attributes(attrs, keep_all=False)

        assert old_result == new_result


class TestTrimElement:
    """Test trim_element correctness after optimization."""

    def test_removes_frame_fields(self):
        el = _make_element("test1")
        trim_element(el)
        assert "frame" not in el
        assert "frame_index" not in el

    def test_removes_keep_all_attr(self):
        el = _make_element("test2")
        trim_element(el)
        assert "keepAllAttr" not in el

    def test_removes_empty_text(self):
        el = _make_element("test3", text="")
        trim_element(el)
        assert "text" not in el

    def test_removes_empty_pseudo_text(self):
        el = _make_element("test4")
        el["beforePseudoText"] = ""
        el["afterPseudoText"] = ""
        trim_element(el)
        assert "beforePseudoText" not in el
        assert "afterPseudoText" not in el

    def test_keeps_interactable_id(self):
        el = _make_element("test5", interactable=True)
        trim_element(el)
        assert "id" in el

    def test_removes_non_interactable_id(self):
        el = _make_element("test6", interactable=False)
        el["attributes"].pop("disabled", None)
        el["attributes"].pop("aria-disabled", None)
        el["attributes"].pop("readonly", None)
        el["attributes"].pop("aria-readonly", None)
        el.pop("hoverOnly", None)
        trim_element(el)
        assert "id" not in el

    def test_filters_attributes(self):
        el = _make_element("test7")
        trim_element(el)
        attrs = el.get("attributes", {})
        # Non-reserved attributes should be removed
        assert "class" not in attrs
        assert "data-testid" not in attrs
        assert "style" not in attrs
        # Reserved attributes should remain
        assert "aria-label" in attrs
        assert "type" in attrs

    def test_truncates_long_name(self):
        el = _make_element("test8")
        el["attributes"]["name"] = "x" * 1000
        trim_element(el)
        assert len(el["attributes"]["name"]) == 500

    def test_processes_children(self):
        child = _make_element("child1")
        parent = _make_element("parent1", children=[child])
        trim_element(parent)
        assert "frame" not in child
        assert "keepAllAttr" not in child


class TestTrimElementTreePerformance:
    """Benchmark tests for trim_element_tree at various scales."""

    @pytest.mark.parametrize("num_elements", [100, 1000, 5000])
    def test_trim_performance(self, num_elements: int):
        tree = _make_element_tree(num_elements)
        tree_copy = copy.deepcopy(tree)

        start = time.perf_counter()
        trim_element_tree(tree_copy)
        elapsed = time.perf_counter() - start

        # Log timing for visibility
        print(f"\ntrim_element_tree({num_elements} elements): {elapsed:.4f}s")
        # Should complete in reasonable time (< 1s for 5000 elements)
        assert elapsed < 2.0, f"trim_element_tree took too long: {elapsed:.4f}s for {num_elements} elements"


class TestJsonToHtmlPerformance:
    """Benchmark json_to_html at various scales."""

    @pytest.mark.parametrize("num_elements", [100, 1000, 5000])
    def test_json_to_html_performance(self, num_elements: int):
        tree = _make_element_tree(num_elements)
        # Trim first (like the real pipeline)
        tree = trim_element_tree(copy.deepcopy(tree))

        start = time.perf_counter()
        result = "".join(json_to_html(element) for element in tree)
        elapsed = time.perf_counter() - start

        print(f"\njson_to_html({num_elements} elements): {elapsed:.4f}s, output: {len(result)} chars")
        assert elapsed < 2.0, f"json_to_html took too long: {elapsed:.4f}s for {num_elements} elements"
        assert len(result) > 0

    def test_json_to_html_correctness(self):
        """Verify basic HTML output structure."""
        el = {
            "tagName": "button",
            "id": "btn1",
            "interactable": True,
            "text": "Click me",
            "attributes": {"type": "submit", "aria-label": "Submit"},
            "children": [],
        }
        html = json_to_html(el)
        assert "<button" in html
        assert "Click me" in html
        assert 'type="submit"' in html
        assert "</button>" in html


class TestDequeVsList:
    """Verify deque.popleft() is faster than list.pop(0) for BFS."""

    def test_deque_faster_than_list(self):
        n = 10000
        items = list(range(n))

        # List pop(0)
        lst = list(items)
        start = time.perf_counter()
        while lst:
            lst.pop(0)
        list_time = time.perf_counter() - start

        # Deque popleft
        dq = deque(items)
        start = time.perf_counter()
        while dq:
            dq.popleft()
        deque_time = time.perf_counter() - start

        print(f"\nlist.pop(0): {list_time:.6f}s, deque.popleft(): {deque_time:.6f}s")
        # deque should be significantly faster
        assert deque_time < list_time


class TestBuildElementDict:
    """Test build_element_dict correctness and hash collision handling."""

    def test_basic_dict_building(self):
        elements = [
            {"id": "e1", "tagName": "button", "frame": "main", "attributes": {"type": "submit"}},
            {"id": "e2", "tagName": "input", "frame": "main", "attributes": {"name": "email"}},
        ]
        css_dict, elem_dict, frame_dict, hash_dict, hash_to_ids = build_element_dict(elements)

        assert "e1" in css_dict
        assert "e2" in css_dict
        assert elem_dict["e1"]["tagName"] == "button"
        assert frame_dict["e1"] == "main"
        assert "e1" in hash_dict
        assert "e2" in hash_dict

    def test_hash_collision_uses_append(self):
        """Verify that hash_to_element_ids uses list append (not concat) for collisions."""
        # Two identical elements (same tag, same attrs) will have the same hash
        el1 = {"id": "e1", "tagName": "div", "frame": "main", "attributes": {}}
        el2 = {"id": "e2", "tagName": "div", "frame": "main", "attributes": {}}
        _, _, _, hash_dict, hash_to_ids = build_element_dict([el1, el2])

        h1 = hash_dict["e1"]
        h2 = hash_dict["e2"]
        assert h1 == h2, "Identical elements should produce the same hash"
        assert hash_to_ids[h1] == ["e1", "e2"]


class TestEconomyTreeProcessing:
    """Test the economy tree SVG filtering logic."""

    def test_filters_svg_root(self):
        svg_el = {"tagName": "svg", "children": [{"tagName": "path"}]}
        result = ScrapedPage._process_element_for_economy_tree(svg_el)
        assert result is None

    def test_filters_svg_children(self):
        tree = {
            "tagName": "div",
            "children": [
                {"tagName": "button", "children": []},
                {"tagName": "svg", "children": [{"tagName": "path"}]},
                {"tagName": "span", "children": []},
            ],
        }
        result = ScrapedPage._process_element_for_economy_tree(tree)
        assert result is not None
        children = result["children"]
        assert len(children) == 2
        assert children[0]["tagName"] == "button"
        assert children[1]["tagName"] == "span"

    def test_filters_nested_svg(self):
        tree = {
            "tagName": "div",
            "children": [
                {
                    "tagName": "span",
                    "children": [
                        {"tagName": "svg", "children": []},
                        {"tagName": "a", "children": []},
                    ],
                },
            ],
        }
        result = ScrapedPage._process_element_for_economy_tree(tree)
        assert result is not None
        inner = result["children"][0]
        assert len(inner["children"]) == 1
        assert inner["children"][0]["tagName"] == "a"

    def test_filters_svg_case_insensitive(self):
        """SVG filtering is case-insensitive via .lower()."""
        tree = {
            "tagName": "div",
            "children": [
                {"tagName": "SVG", "children": []},
                {"tagName": "Svg", "children": []},
                {"tagName": "button", "children": []},
            ],
        }
        result = ScrapedPage._process_element_for_economy_tree(tree)
        assert result is not None
        assert len(result["children"]) == 1
        assert result["children"][0]["tagName"] == "button"

    def test_no_children(self):
        el = {"tagName": "input"}
        result = ScrapedPage._process_element_for_economy_tree(el)
        assert result is not None
        assert result["tagName"] == "input"
