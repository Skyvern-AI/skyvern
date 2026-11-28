"""Regression tests for cursor positioning in drag/left_mouse.

Both functions gated the move-to-start on a truthiness check, so a coordinate
of 0 (a screen edge) was treated as "no coordinate" and the cursor was never
moved before the mouse press. The drag/click then fired at the previous cursor
position instead of the requested edge coordinate.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.actions import handler_utils


def _fake_page() -> MagicMock:
    page = MagicMock()
    page.mouse.down = AsyncMock()
    page.mouse.up = AsyncMock()
    return page


@pytest.mark.asyncio
async def test_drag_moves_cursor_to_zero_start() -> None:
    page = _fake_page()
    with (
        patch.object(handler_utils.EventStrategyFactory, "move_cursor", new=AsyncMock()) as move_cursor,
        patch.object(handler_utils.EventStrategyFactory, "sync_cursor_position", new=MagicMock()),
    ):
        await handler_utils.drag(page, start_x=0, start_y=0)
    move_cursor.assert_awaited_once_with(page, 0, 0)


@pytest.mark.asyncio
async def test_drag_moves_cursor_to_zero_x_nonzero_y() -> None:
    page = _fake_page()
    with (
        patch.object(handler_utils.EventStrategyFactory, "move_cursor", new=AsyncMock()) as move_cursor,
        patch.object(handler_utils.EventStrategyFactory, "sync_cursor_position", new=MagicMock()),
    ):
        await handler_utils.drag(page, start_x=0, start_y=5)
    move_cursor.assert_awaited_once_with(page, 0, 5)


@pytest.mark.asyncio
async def test_drag_skips_move_when_start_missing() -> None:
    page = _fake_page()
    with patch.object(handler_utils.EventStrategyFactory, "move_cursor", new=AsyncMock()) as move_cursor:
        await handler_utils.drag(page, start_x=None, start_y=None, path=[(3, 4)])
    # move_cursor still runs for the path point, but never for a None start.
    for call in move_cursor.await_args_list:
        assert call.args[1:] != (None, None)


@pytest.mark.asyncio
async def test_left_mouse_moves_cursor_to_zero() -> None:
    page = _fake_page()
    with patch.object(handler_utils.EventStrategyFactory, "move_cursor", new=AsyncMock()) as move_cursor:
        await handler_utils.left_mouse(page, x=0, y=0, direction="down")
    move_cursor.assert_awaited_once_with(page, 0, 0)
    page.mouse.down.assert_awaited_once()


@pytest.mark.asyncio
async def test_left_mouse_skips_move_when_coordinates_missing() -> None:
    page = _fake_page()
    with patch.object(handler_utils.EventStrategyFactory, "move_cursor", new=AsyncMock()) as move_cursor:
        await handler_utils.left_mouse(page, x=None, y=None, direction="up")
    move_cursor.assert_not_awaited()
    page.mouse.up.assert_awaited_once()
