"""SKY-9718 Layer 1 — deterministic element-tree compression unit tests.

Operates on the JSON element-tree shape (mirror of `_process_element_for_economy_tree`
in scraped_page.py). Each of the 3 transforms is independently gated; tests
exercise each in isolation and a realistic end-to-end combo.
"""

import copy

from skyvern.utils.lean_html import apply_lean_to_tree


def _node(
    tag: str, *, id: str | None = None, attributes: dict | None = None, children: list | None = None, **extra
) -> dict:
    """Build an element-tree node in the shape ScrapedPage produces."""
    n: dict = {"tagName": tag, "attributes": dict(attributes or {}), "children": list(children or [])}
    if id is not None:
        n["id"] = id
    n.update(extra)
    return n


# Convenience constant for tests that want to enable every transform.
ALL_LEAN_FLAGS = dict(
    compress_long_href=True,
    compress_image_src=True,
    strip_url_query_strings=True,
)


# --- flag #1: compress_long_href -----------------------------------------


def test_compress_long_href_replaces_with_templated() -> None:
    """Hrefs > 150 chars get replaced with '#templated' (short-circuits json_to_html's sha256 substitution)."""
    long_href = "https://example.com/path/with/very/long/" + "x" * 200
    assert len(long_href) > 150
    tree = [_node("a", attributes={"href": long_href}, children=[])]
    out = apply_lean_to_tree(tree, compress_long_href=True)
    assert out[0]["attributes"]["href"] == "#templated"


def test_compress_long_href_short_url_left_alone() -> None:
    tree = [_node("a", attributes={"href": "/login"}, children=[])]
    out = apply_lean_to_tree(tree, compress_long_href=True)
    assert out[0]["attributes"]["href"] == "/login"


def test_compress_long_href_off_keeps_long_url() -> None:
    long_href = "https://example.com/" + "x" * 200
    tree = [_node("a", attributes={"href": long_href}, children=[])]
    out = apply_lean_to_tree(tree, compress_long_href=False)
    assert out[0]["attributes"]["href"] == long_href


def test_strip_query_runs_before_long_href_so_short_paths_survive() -> None:
    """Regression: a short path with a giant query string should keep its path,
    not collapse to '#templated'. Strip-QS runs first, then long-href checks length."""
    short_path = "https://x.co/foo"
    long_query = "?" + "utm=" + "x" * 250
    assert len(short_path + long_query) > 150
    tree = [_node("a", attributes={"href": short_path + long_query}, children=[])]
    out = apply_lean_to_tree(tree, compress_long_href=True, strip_url_query_strings=True)
    # Path preserved; long-href no-ops because the post-strip URL is short.
    assert out[0]["attributes"]["href"] == short_path


def test_compress_long_href_still_fires_when_path_alone_exceeds_threshold() -> None:
    """A URL that's long even after the query is stripped still gets hashed."""
    long_path = "https://example.com/" + "x" * 200
    tree = [_node("a", attributes={"href": long_path + "?ignored=1"}, children=[])]
    out = apply_lean_to_tree(tree, compress_long_href=True, strip_url_query_strings=True)
    assert out[0]["attributes"]["href"] == "#templated"


# --- flag #2: compress_image_src -----------------------------------------


def test_compress_image_src_drops_src_keeps_alt_and_id() -> None:
    tree = [
        _node(
            "img",
            id="AEE0",
            attributes={"src": "https://cdn.example.com/very/long/path/image.jpg", "alt": "A pretty cat"},
            children=[],
        )
    ]
    out = apply_lean_to_tree(tree, compress_image_src=True)
    assert "src" not in out[0]["attributes"]
    assert out[0]["attributes"]["alt"] == "A pretty cat"
    assert out[0]["id"] == "AEE0"


def test_compress_image_src_does_not_touch_non_img_src() -> None:
    """Only `<img>` src is dropped; <script src>, <iframe src> etc are left alone."""
    tree = [
        _node("script", attributes={"src": "https://cdn.example.com/foo.js"}, children=[]),
        _node("iframe", attributes={"src": "https://example.com/frame"}, children=[]),
    ]
    out = apply_lean_to_tree(tree, compress_image_src=True)
    assert out[0]["attributes"]["src"] == "https://cdn.example.com/foo.js"
    assert out[1]["attributes"]["src"] == "https://example.com/frame"


def test_compress_image_src_off_keeps_src() -> None:
    tree = [_node("img", attributes={"src": "/cat.jpg", "alt": "cat"}, children=[])]
    out = apply_lean_to_tree(tree, compress_image_src=False)
    assert out[0]["attributes"]["src"] == "/cat.jpg"


# --- flag #3: strip_url_query_strings ------------------------------------


def test_strip_url_query_strings_href() -> None:
    tree = [_node("a", attributes={"href": "https://example.com/foo?utm_source=x&utm_medium=y"}, children=[])]
    out = apply_lean_to_tree(tree, strip_url_query_strings=True)
    assert out[0]["attributes"]["href"] == "https://example.com/foo"


def test_strip_url_query_strings_src() -> None:
    """Strips ?... from `src` too. Combined with compress_image_src=False so we can observe."""
    tree = [_node("script", attributes={"src": "/path/foo.js?v=12345"}, children=[])]
    out = apply_lean_to_tree(tree, strip_url_query_strings=True, compress_image_src=False)
    assert out[0]["attributes"]["src"] == "/path/foo.js"


def test_strip_url_query_strings_leaves_clean_url_alone() -> None:
    tree = [_node("a", attributes={"href": "/clean/path"}, children=[])]
    out = apply_lean_to_tree(tree, strip_url_query_strings=True)
    assert out[0]["attributes"]["href"] == "/clean/path"


def test_strip_url_query_strings_off_keeps_query() -> None:
    tree = [_node("a", attributes={"href": "/x?utm=1"}, children=[])]
    out = apply_lean_to_tree(tree, strip_url_query_strings=False)
    assert out[0]["attributes"]["href"] == "/x?utm=1"


# --- defaults / no-ops ---------------------------------------------------


def test_default_flags_off_is_a_deep_copy() -> None:
    """With every flag off, the recipe is a deep copy (no mutations)."""
    tree = [_node("a", id="AAAB", attributes={"href": "/x?utm=1"}, children=[])]
    out = apply_lean_to_tree(tree)
    assert out == tree
    # And it really is a copy:
    out[0]["id"] = "MUTATED"
    assert tree[0]["id"] == "AAAB"


def test_apply_lean_to_tree_does_not_mutate_input() -> None:
    tree = [_node("a", id="AAAB", attributes={"href": "/x?utm=1"}, children=[])]
    snapshot = copy.deepcopy(tree)
    apply_lean_to_tree(tree, **ALL_LEAN_FLAGS)
    assert tree == snapshot, "apply_lean_to_tree must deep-copy and leave the input untouched"


def test_apply_lean_to_tree_is_idempotent_all_flags() -> None:
    tree = [
        _node(
            "div",
            id="AAA",
            attributes={},
            children=[
                _node(
                    "a",
                    id="AAB",
                    attributes={"href": "/x?utm=1"},
                    children=[_node("img", attributes={"src": "//cdn/i.png?w=4", "alt": "x"}, children=[])],
                )
            ],
        )
    ]
    once = apply_lean_to_tree(tree, **ALL_LEAN_FLAGS)
    twice = apply_lean_to_tree(once, **ALL_LEAN_FLAGS)
    assert once == twice


def test_apply_lean_to_tree_preserves_skyvern_internal_id() -> None:
    """Skyvern internal IDs (`element["id"]`) are NOT touched by this module.
    Callers drop them via `html_need_skyvern_attrs=False` at the render layer.
    """
    tree = [_node("a", id="AAAB", attributes={"href": "/x"}, children=[])]
    out = apply_lean_to_tree(tree, **ALL_LEAN_FLAGS)
    assert out[0]["id"] == "AAAB"


# --- end-to-end combo ----------------------------------------------------


def test_realistic_snippet_all_flags_on() -> None:
    """Every transform fires on a realistic snippet. Skyvern IDs untouched at this layer."""
    tree = [
        _node(
            "div",
            id="AAAP",
            attributes={},
            children=[
                _node(
                    "a",
                    id="AAAF",
                    attributes={
                        "href": "https://www.example.com/business?utm_content=x&utm_campaign=y&utm_source=z",
                    },
                    children=[_node("span", id="AAAG", text="Businesses", children=[])],
                ),
                _node(
                    "img",
                    id="AEE0",
                    attributes={
                        "src": "https://cdn.example.com/api/utilities/v1/imageproxy/x.jpg?w=388",
                        "alt": "logo",
                    },
                    children=[],
                ),
                _node(
                    "a",
                    id="AAAL",
                    attributes={"href": "https://www.example.com/very/long/path/" + "x" * 200},
                    children=[],
                ),
            ],
        )
    ]
    out = apply_lean_to_tree(tree, **ALL_LEAN_FLAGS)
    # Skyvern IDs survive — that's `html_need_skyvern_attrs`'s job, not ours.
    for node in [out[0], out[0]["children"][0], out[0]["children"][1], out[0]["children"][2]]:
        assert node.get("id") is not None
    # URL query stripped on the first <a>
    assert out[0]["children"][0]["attributes"]["href"] == "https://www.example.com/business"
    # img src dropped
    assert "src" not in out[0]["children"][1]["attributes"]
    # hashed-href compressed
    assert out[0]["children"][2]["attributes"]["href"] == "#templated"
