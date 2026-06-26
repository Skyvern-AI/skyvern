"""Default scrape callers must not invoke visual bounding box helpers.

Exercises ``_scrolling_screenshots_helper`` with ``draw_boxes=False`` (the new
default) and asserts that no overlay build/remove call lands on the page.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skyvern.webeye.utils.page as page_module
from skyvern.webeye.utils.page import ScreenshotMode


class _StubSkyvernFrame:
    """In-memory stand-in for SkyvernFrame that records every call."""

    def __init__(self, scroll_steps: list[float], scroll_heights: list[int]) -> None:
        self._scroll_steps = list(scroll_steps)
        self._scroll_heights = list(scroll_heights)
        self.calls: list[str] = []

    async def is_window_scrollable(self) -> bool:
        return True

    async def get_scroll_width_and_height(self) -> tuple[int, int]:
        height = self._scroll_heights.pop(0) if self._scroll_heights else 1000
        return (800, height)

    async def scroll_to_top(self, *, draw_boxes: bool, frame: str, frame_index: int) -> float:
        self.calls.append(f"scroll_to_top(draw_boxes={draw_boxes})")
        return self._scroll_steps.pop(0) if self._scroll_steps else 0.0

    async def scroll_to_next_page(
        self,
        *,
        draw_boxes: bool,
        frame: str,
        frame_index: int,
        need_overlap: bool = True,
    ) -> float:
        self.calls.append(f"scroll_to_next_page(draw_boxes={draw_boxes})")
        return self._scroll_steps.pop(0) if self._scroll_steps else 0.0

    async def build_tree_from_body(self, frame_name: str, frame_index: int) -> None:
        self.calls.append("build_tree_from_body")

    async def safe_wait_for_animation_end(self, caller: str) -> None:
        self.calls.append(f"safe_wait_for_animation_end({caller})")

    async def build_elements_and_draw_bounding_boxes(self, frame: str, frame_index: int) -> None:
        self.calls.append("build_elements_and_draw_bounding_boxes")

    async def remove_bounding_boxes(self) -> None:
        self.calls.append("remove_bounding_boxes")


@pytest.mark.asyncio
async def test_scrolling_helper_skips_overlay_when_draw_boxes_false() -> None:
    """With the deprecated overlay default (False), neither build nor remove fires."""

    stub = _StubSkyvernFrame(
        # scroll positions: top(0) → page1(700) → page2(1400) → terminal(1400)
        scroll_steps=[0.0, 700.0, 1400.0, 1400.0],
        # consistent scroll heights => no element-tree rebuild
        scroll_heights=[2000, 2000, 2000, 2000],
    )
    page = MagicMock(name="page")

    async def _fake_screenshot(page, mode):  # noqa: D401, ARG001
        return b"snap"

    with (
        patch.object(page_module.SkyvernFrame, "create_instance", AsyncMock(return_value=stub)),
        patch.object(page_module, "_current_viewpoint_screenshot_helper", _fake_screenshot),
    ):
        screenshots, positions = await page_module._scrolling_screenshots_helper(
            page=page,
            url="https://example.com",
            draw_boxes=False,
            max_number=4,
            mode=ScreenshotMode.DETAILED,
        )

    # We still scrolled + captured screenshots normally.
    assert len(screenshots) >= 1
    assert positions == sorted(positions)
    # No overlay build or removal fired.
    assert "build_elements_and_draw_bounding_boxes" not in stub.calls
    assert "remove_bounding_boxes" not in stub.calls
    # Both scroll calls forwarded draw_boxes=False explicitly.
    assert any("scroll_to_top(draw_boxes=False)" in c for c in stub.calls)
    assert all("draw_boxes=True" not in c for c in stub.calls)


@pytest.mark.asyncio
async def test_scrolling_helper_non_scrollable_page_skips_overlay() -> None:
    """The non-scrollable branch must also avoid overlay build/remove by default."""

    stub = _StubSkyvernFrame(scroll_steps=[], scroll_heights=[])

    async def _is_window_scrollable() -> bool:
        return False

    stub.is_window_scrollable = _is_window_scrollable  # type: ignore[method-assign]

    page = MagicMock(name="page")

    async def _fake_screenshot(page, mode):  # noqa: D401, ARG001
        return b"snap"

    with (
        patch.object(page_module.SkyvernFrame, "create_instance", AsyncMock(return_value=stub)),
        patch.object(page_module, "_current_viewpoint_screenshot_helper", _fake_screenshot),
    ):
        screenshots, positions = await page_module._scrolling_screenshots_helper(
            page=page,
            url="https://example.com",
            draw_boxes=False,
            max_number=1,
            mode=ScreenshotMode.DETAILED,
        )

    assert screenshots == [b"snap"]
    assert positions == [0]
    assert "build_elements_and_draw_bounding_boxes" not in stub.calls
    assert "remove_bounding_boxes" not in stub.calls
