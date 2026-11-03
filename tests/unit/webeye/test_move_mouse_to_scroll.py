"""SkyvernElement.move_mouse_to must scroll the element into the live viewport
*before* reading its bounding box (SKY-15195).

The incident (the reported staging run) generated a cursor target from a stale
document-level bounding box (y ~ 5044) because the setup pre-move read the box
before any scroll. move_mouse_to must resolve the element into view first so the
re-read box is a viewport coordinate, never an off-screen one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.utils import dom as dom_module
from skyvern.webeye.utils.dom import SkyvernElement


def _make_element(order: list[str], bbox: dict) -> SkyvernElement:
    element = SkyvernElement.__new__(SkyvernElement)

    locator = MagicMock()

    async def _bbox(*_args, **_kwargs) -> dict:
        order.append("bbox")
        return bbox

    async def _scroll_if_needed(*_args, **_kwargs) -> None:
        order.append("scroll")

    locator.bounding_box = AsyncMock(side_effect=_bbox)
    locator.scroll_into_view_if_needed = AsyncMock(side_effect=_scroll_if_needed)

    async def _scroll_into_view(*_args, **_kwargs) -> None:
        order.append("scroll")

    element.get_locator = MagicMock(return_value=locator)  # type: ignore[method-assign]
    element.get_id = MagicMock(return_value="el-under-test")  # type: ignore[method-assign]
    element.scroll_into_view = AsyncMock(side_effect=_scroll_into_view)  # type: ignore[method-assign]
    return element


@pytest.mark.asyncio
async def test_move_mouse_to_scrolls_before_reading_bounding_box() -> None:
    order: list[str] = []
    element = _make_element(order, {"x": 300, "y": 400, "width": 40, "height": 20})
    page = MagicMock()

    with patch.object(dom_module.EventStrategyFactory, "move_cursor", new=AsyncMock()):
        await element.move_mouse_to(page)

    assert "scroll" in order, "move_mouse_to must scroll the element into view"
    assert "bbox" in order, "move_mouse_to must read the bounding box"
    assert order.index("scroll") < order.index("bbox"), f"scroll must precede bounding-box read; got {order}"


@pytest.mark.asyncio
async def test_move_mouse_to_dispatches_rescrolled_in_viewport_coordinate() -> None:
    # After scroll the re-read box is in-viewport; the cursor target handed to
    # the active strategy must be that in-viewport coordinate.
    order: list[str] = []
    element = _make_element(order, {"x": 300, "y": 400, "width": 40, "height": 20})
    page = MagicMock()

    with patch.object(dom_module.EventStrategyFactory, "move_cursor", new=AsyncMock()) as move_cursor:
        dest_x, dest_y = await element.move_mouse_to(page)

    move_cursor.assert_awaited_once()
    called_x, called_y = move_cursor.await_args.args[1], move_cursor.await_args.args[2]
    assert 300 <= called_x <= 340
    assert 400 <= called_y <= 420
    assert (called_x, called_y) == (dest_x, dest_y)
