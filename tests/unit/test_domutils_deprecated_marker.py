"""Source-contract test for the deprecated visual bounding box overlay helpers.

The visual overlay helpers in ``domUtils.js`` are retained briefly for backwards
compatibility and are scheduled for removal. Future cleanup should be
intentional, so this test pins two contracts:

1. The helpers still exist (we have not silently deleted them mid-deprecation).
2. Each helper carries a clear ``DEPRECATED`` marker so the next reader knows
   the path is on its way out.
"""

from __future__ import annotations

from pathlib import Path

_DOM_UTILS = Path(__file__).resolve().parents[2] / "skyvern" / "webeye" / "scraper" / "domUtils.js"


DEPRECATED_OVERLAY_HELPERS = (
    "drawBoundingBoxes",
    "buildElementsAndDrawBoundingBoxes",
    "removeBoundingBoxes",
)


def _function_decl_line(source_lines: list[str], helper: str) -> int:
    """Return 1-based line number of the helper's ``function`` declaration."""

    for idx, line in enumerate(source_lines):
        if f"function {helper}(" in line:
            return idx + 1
    raise AssertionError(f"could not find function declaration for {helper}() in domUtils.js")


def test_deprecated_overlay_helpers_still_present() -> None:
    source = _DOM_UTILS.read_text()
    for helper in DEPRECATED_OVERLAY_HELPERS:
        assert f"function {helper}(" in source, f"{helper} was unexpectedly removed before its scheduled cleanup"


def test_deprecated_overlay_helpers_have_marker_comment() -> None:
    source_lines = _DOM_UTILS.read_text().splitlines()
    for helper in DEPRECATED_OVERLAY_HELPERS:
        decl_line = _function_decl_line(source_lines, helper)
        # Look at the 6 lines preceding the declaration for a DEPRECATED marker.
        preceding = source_lines[max(0, decl_line - 7) : decl_line - 1]
        assert any("DEPRECATED" in line for line in preceding), (
            f"{helper}() must carry a DEPRECATED marker in the preceding comment block; checked lines: {preceding!r}"
        )
