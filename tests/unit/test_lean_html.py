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
    compress_nonnavigable_href=True,
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


# --- flag #4: compress_nonnavigable_href ---------------------------------


def test_nonnavigable_drops_javascript_href() -> None:
    tree = [_node("a", id="AABm", attributes={"href": "javascript:void(0)"}, text="More", children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert "href" not in out[0]["attributes"]
    # tag identity, id, and text survive — element stays clickable-by-id.
    assert out[0]["id"] == "AABm"
    assert out[0]["text"] == "More"


def test_nonnavigable_drops_labeled_webforms_postback_href() -> None:
    """A labeled __doPostBack anchor (visible text present) drops the opaque token."""
    href = "javascript:__doPostBack('DataListResultats$ctl02$lnkDetail','')"
    tree = [_node("a", id="AABm", attributes={"href": href}, text="+ Details", children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert "href" not in out[0]["attributes"]


def test_nonnavigable_keeps_textless_postback_href_stripped() -> None:
    """A textless icon link's only naming signal is the postback control name, so
    it survives — but with the noisy `javascript:` wrapper stripped (semantic
    payload preserved, token cost reduced)."""
    href = "javascript:__doPostBack('ctl00$grid$lnkDownloadPDF','')"
    tree = [_node("a", id="AA", attributes={"href": href}, text="", children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert out[0]["attributes"]["href"] == "__doPostBack('ctl00$grid$lnkDownloadPDF','')"


def test_nonnavigable_keeps_icon_marker_only_postback_href_stripped() -> None:
    """`[icon]` is not a human label, so an icon-only postback link keeps its
    semantic payload (with the `javascript:` wrapper stripped)."""
    href = "javascript:__doPostBack('ctl00$grid$lnkExport','')"
    tree = [_node("a", id="AA", attributes={"href": href}, text="[icon]", children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert out[0]["attributes"]["href"] == "__doPostBack('ctl00$grid$lnkExport','')"


def test_nonnavigable_keeps_textless_void_wrapped_call_stripped() -> None:
    """A `javascript:void(downloadFn(...))` href on a textless control keeps the
    inner function-name signal — Codex/Lawy regression."""
    tree = [_node("a", id="AA", attributes={"href": "javascript:void(downloadPdf('123'))"}, text="", children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert out[0]["attributes"]["href"] == "downloadPdf('123')"


def test_nonnavigable_keeps_textless_bare_javascript_expression_stripped() -> None:
    """A bare `javascript:fn(...)` href (no void wrapper) on a textless control
    keeps the function call after the scheme is stripped."""
    tree = [_node("a", id="AA", attributes={"href": "javascript:openModal('confirm')"}, text="", children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert out[0]["attributes"]["href"] == "openModal('confirm')"


def test_nonnavigable_drops_textless_javascript_no_op_payloads() -> None:
    """`javascript:void(0)`, `javascript:`, `javascript:void(undefined)` etc.
    carry no signal — drop the href entirely even on textless controls."""
    for noop in (
        "javascript:",
        "javascript:void(0)",
        "javascript:void(0);",
        "javascript:void(undefined)",
        "javascript:void(null)",
        "javascript:;",
    ):
        tree = [_node("a", id="AA", attributes={"href": noop}, text="", children=[])]
        out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
        assert "href" not in out[0]["attributes"], f"{noop!r} should drop"


def test_nonnavigable_drops_postback_href_labeled_by_aria() -> None:
    href = "javascript:__doPostBack('ctl00$grid$lnkExport','')"
    tree = [_node("a", id="AA", attributes={"href": href, "aria-label": "Export"}, text="", children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert "href" not in out[0]["attributes"]


def test_nonnavigable_drops_postback_href_labeled_by_child() -> None:
    """A wrapper anchor with the label in a child span still drops the postback href."""
    href = "javascript:__doPostBack('ctl00$grid$lnkDetail','')"
    tree = [_node("a", id="AA", attributes={"href": href}, children=[_node("span", id="AB", text="Details")])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert "href" not in out[0]["attributes"]


def test_nonnavigable_keeps_href_when_label_is_below_recursion_depth_cap() -> None:
    """Depth guard: a label nested deeper than _TEXT_SIGNAL_MAX_DEPTH isn't detected,
    so the anchor is treated as textless and the semantic payload is conservatively
    KEPT (no spurious drop, no RecursionError on deep DOMs). The `javascript:`
    wrapper is still stripped on the textless path."""
    from skyvern.utils.lean_html import _TEXT_SIGNAL_MAX_DEPTH

    href = "javascript:__doPostBack('ctl00$grid$lnkDeep','')"
    node: dict = _node("span", id="ZZ", text="Details")
    for _ in range(_TEXT_SIGNAL_MAX_DEPTH + 5):
        node = _node("div", id="WR", children=[node])
    anchor = _node("a", id="AA", attributes={"href": href}, text="", children=[node])
    out = apply_lean_to_tree([anchor], compress_nonnavigable_href=True)
    assert out[0]["attributes"]["href"] == "__doPostBack('ctl00$grid$lnkDeep','')"


def test_nonnavigable_drops_pure_idiom_even_when_textless() -> None:
    """Pure idioms (#/empty/javascript:;/void(0)) are content-free, so they drop
    regardless of whether the element has a label."""
    for idiom in ("#", "", "javascript:;", "javascript:void(0)", "javascript:void(0);"):
        tree = [_node("a", id="AA", attributes={"href": idiom}, text="", children=[])]
        out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
        assert "href" not in out[0]["attributes"], f"{idiom!r} should drop even on a textless element"


def test_nonnavigable_drops_empty_and_bare_hash() -> None:
    for noop in ("", "#", "  "):
        tree = [_node("a", id="AA", attributes={"href": noop}, children=[])]
        out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
        assert "href" not in out[0]["attributes"], f"{noop!r} should be dropped"


def test_nonnavigable_preserves_hash_routes_and_anchors() -> None:
    """SPA hash routes (#/...) and same-page anchors (#name) are real destinations — keep them."""
    for navigable in ("#/checkout", "#/orders/123", "#section", "#" + "x"):
        tree = [_node("a", id="AA", attributes={"href": navigable}, children=[])]
        out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
        assert out[0]["attributes"]["href"] == navigable, f"{navigable!r} must be preserved"


def test_nonnavigable_preserves_real_urls_even_when_long() -> None:
    long_url = "https://www.example.com/very/long/destination/" + "x" * 200
    tree = [_node("a", id="AA", attributes={"href": long_url}, children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert out[0]["attributes"]["href"] == long_url


def test_nonnavigable_substring_in_navigable_url_is_not_dropped() -> None:
    """RISK-4 regression: the rule is scheme-anchored, not a bare substring match.
    A real http(s) URL whose path merely contains '__doPostBack' must survive."""
    url = "https://docs.example.com/help/__doPostBack-explained"
    tree = [_node("a", id="AA", attributes={"href": url}, children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=True)
    assert out[0]["attributes"]["href"] == url


def test_nonnavigable_off_keeps_href() -> None:
    tree = [_node("a", id="AA", attributes={"href": "javascript:void(0)"}, children=[])]
    out = apply_lean_to_tree(tree, compress_nonnavigable_href=False)
    assert out[0]["attributes"]["href"] == "javascript:void(0)"


def test_nonnavigable_is_token_monotonic_never_adds_bytes() -> None:
    """The transform only ever removes the attribute — it must never replace a short
    href with a longer marker (that would inflate tokens, the inverse of the goal)."""
    from skyvern.forge.sdk.core import skyvern_context
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
    from skyvern.webeye.scraper.scraped_page import json_to_html

    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        for noop in ("#", "", "javascript:__doPostBack('x','')"):
            node = _node("a", id="AA", attributes={"href": noop}, text="t", children=[])
            before = json_to_html(copy.deepcopy(node), need_skyvern_attrs=False)
            after = json_to_html(
                apply_lean_to_tree([node], compress_nonnavigable_href=True)[0], need_skyvern_attrs=False
            )
            assert len(after) <= len(before), f"href={noop!r}: rendered HTML grew ({before!r} -> {after!r})"


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
