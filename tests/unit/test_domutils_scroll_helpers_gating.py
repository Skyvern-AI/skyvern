"""JS-source contract: scroll helpers must not touch overlay code when draw_boxes is false.

This complements ``test_scrolling_screenshots_skip_overlay`` (which stubs the
Python side). Here we parse the real ``domUtils.js`` source and assert that
every overlay helper call inside ``safeScrollToTop`` and ``scrollToNextPage``
is nested inside an ``if (draw_boxes) { ... }`` branch. Without this gate, the
default ``draw_boxes=false`` path would still call ``removeBoundingBoxes()`` and
could mutate a target page that happens to own a ``#boundingBoxContainer``.
"""

from __future__ import annotations

from pathlib import Path

_DOM_UTILS = Path(__file__).resolve().parents[2] / "skyvern" / "webeye" / "scraper" / "domUtils.js"

# Helpers that must only fire on the overlay path.
_OVERLAY_HELPERS = (
    "removeBoundingBoxes",
    "buildElementsAndDrawBoundingBoxes",
    "drawBoundingBoxes",
)

# Scroll helpers whose default callers reach with draw_boxes=false.
_SCROLL_HELPERS = ("safeScrollToTop", "scrollToNextPage")


def _extract_function_body(source: str, fn_name: str) -> str:
    """Return the source of ``async function <fn_name>(...) { ... }``.

    Brace-matches from the first ``{`` after the declaration to the balanced
    closing ``}``. This is sufficient for the hand-written scroll helpers and
    keeps the test self-contained (no JS parser dependency).
    """

    needle = f"async function {fn_name}("
    start = source.find(needle)
    assert start != -1, f"could not find async function {fn_name}() in domUtils.js"

    brace_start = source.find("{", start)
    assert brace_start != -1, f"could not find opening brace for {fn_name}()"

    depth = 0
    for idx in range(brace_start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[brace_start : idx + 1]

    raise AssertionError(f"unbalanced braces in {fn_name}() body")


def _scopes_inside_if_draw_boxes(body: str) -> list[tuple[int, int]]:
    """Return [(open_index, close_index)] ranges for each ``if (draw_boxes) {...}`` block."""

    needle = "if (draw_boxes) {"
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        loc = body.find(needle, cursor)
        if loc == -1:
            return ranges
        brace_open = loc + len(needle) - 1
        depth = 0
        for idx in range(brace_open, len(body)):
            ch = body[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    ranges.append((brace_open, idx))
                    cursor = idx + 1
                    break
        else:
            raise AssertionError("unbalanced braces inside if (draw_boxes) block")


def _call_sites(body: str, helper: str) -> list[int]:
    """Locations of ``<helper>(`` calls inside ``body``."""

    needle = f"{helper}("
    locs: list[int] = []
    cursor = 0
    while True:
        loc = body.find(needle, cursor)
        if loc == -1:
            return locs
        locs.append(loc)
        cursor = loc + len(needle)


def test_scroll_helpers_gate_overlay_calls_under_draw_boxes() -> None:
    source = _DOM_UTILS.read_text()
    failures: list[str] = []

    for fn_name in _SCROLL_HELPERS:
        body = _extract_function_body(source, fn_name)
        gated_ranges = _scopes_inside_if_draw_boxes(body)
        for helper in _OVERLAY_HELPERS:
            for call_idx in _call_sites(body, helper):
                if not any(open_idx < call_idx < close_idx for open_idx, close_idx in gated_ranges):
                    failures.append(
                        f"{fn_name}() invokes {helper}() outside an `if (draw_boxes)` block "
                        f"(offset {call_idx} in function body)"
                    )

    assert not failures, "Overlay helpers must only run when draw_boxes is true:\n" + "\n".join(failures)


def test_default_scrape_pipeline_passes_draw_boxes_false_to_scroll_helpers() -> None:
    """Sanity check the Python -> JS argument passing matches the gated contract."""

    page_src = (Path(__file__).resolve().parents[2] / "skyvern" / "webeye" / "utils" / "page.py").read_text()

    # Both scroll wrappers exist and forward draw_boxes verbatim into the JS expression.
    assert "safeScrollToTop(draw_boxes, frame, frame_index)" in page_src
    assert "scrollToNextPage(draw_boxes, frame, frame_index, need_overlap)" in page_src

    # And the helper that orchestrates the scroll loop defaults to False.
    assert "draw_boxes: bool = False" in page_src
