"""Tests for handler_utils coordinate handling, specifically (0, 0) as valid position.

The original code used `if x and y:` which incorrectly treated 0 as falsy,
rejecting the valid coordinate (0, 0) - the top-left corner of the viewport.

These tests verify that (0, 0) is treated as a valid coordinate position.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.actions import handler_utils


@pytest.mark.asyncio
async def test_drag_with_zero_coordinates() -> None:
    """Verify drag() accepts (0, 0) as valid start coordinates.

    (0, 0) is the top-left corner of the viewport - a legitimate position.
    """
    mock_page = MagicMock()
    mock_page.mouse.down = AsyncMock()
    mock_page.mouse.up = AsyncMock()

    with patch.object(handler_utils, "EventStrategyFactory") as mock_factory:
        mock_factory.move_cursor = AsyncMock()

        await handler_utils.drag(page=mock_page, start_x=0, start_y=0)

        # move_cursor MUST be called with (0, 0) - not skipped
        mock_factory.move_cursor.assert_called_once_with(mock_page, 0, 0)
        mock_page.mouse.down.assert_called_once()
        mock_page.mouse.up.assert_called_once()


@pytest.mark.asyncio
async def test_drag_without_coordinates() -> None:
    """Verify drag() skips move_cursor when coordinates are None."""
    mock_page = MagicMock()
    mock_page.mouse.down = AsyncMock()
    mock_page.mouse.up = AsyncMock()

    with patch.object(handler_utils, "EventStrategyFactory") as mock_factory:
        mock_factory.move_cursor = AsyncMock()

        await handler_utils.drag(page=mock_page, start_x=None, start_y=None)

        # move_cursor must NOT be called when coordinates are None
        mock_factory.move_cursor.assert_not_called()
        mock_page.mouse.down.assert_called_once()
        mock_page.mouse.up.assert_called_once()


@pytest.mark.asyncio
async def test_left_mouse_down_with_zero_coordinates() -> None:
    """Verify left_mouse() accepts (0, 0) as valid position for mouse down."""
    mock_page = MagicMock()
    mock_page.mouse.down = AsyncMock()

    with patch.object(handler_utils, "EventStrategyFactory") as mock_factory:
        mock_factory.move_cursor = AsyncMock()

        await handler_utils.left_mouse(page=mock_page, x=0, y=0, direction="down")

        # move_cursor MUST be called with (0, 0) - not skipped
        mock_factory.move_cursor.assert_called_once_with(mock_page, 0, 0)
        mock_page.mouse.down.assert_called_once()


@pytest.mark.asyncio
async def test_left_mouse_up_with_zero_coordinates() -> None:
    """Verify left_mouse() accepts (0, 0) as valid position for mouse up."""
    mock_page = MagicMock()
    mock_page.mouse.up = AsyncMock()

    with patch.object(handler_utils, "EventStrategyFactory") as mock_factory:
        mock_factory.move_cursor = AsyncMock()

        await handler_utils.left_mouse(page=mock_page, x=0, y=0, direction="up")

        # move_cursor MUST be called with (0, 0) - not skipped
        mock_factory.move_cursor.assert_called_once_with(mock_page, 0, 0)
        mock_page.mouse.up.assert_called_once()


@pytest.mark.asyncio
async def test_left_mouse_without_coordinates() -> None:
    """Verify left_mouse() skips move_cursor when coordinates are None."""
    mock_page = MagicMock()
    mock_page.mouse.down = AsyncMock()

    with patch.object(handler_utils, "EventStrategyFactory") as mock_factory:
        mock_factory.move_cursor = AsyncMock()

        await handler_utils.left_mouse(page=mock_page, x=None, y=None, direction="down")

        # move_cursor must NOT be called when coordinates are None
        mock_factory.move_cursor.assert_not_called()
        mock_page.mouse.down.assert_called_once()


@pytest.mark.asyncio
async def test_drag_with_positive_coordinates() -> None:
    """Verify drag() still works with normal positive coordinates."""
    mock_page = MagicMock()
    mock_page.mouse.down = AsyncMock()
    mock_page.mouse.up = AsyncMock()

    with patch.object(handler_utils, "EventStrategyFactory") as mock_factory:
        mock_factory.move_cursor = AsyncMock()

        await handler_utils.drag(page=mock_page, start_x=100, start_y=200)

        mock_factory.move_cursor.assert_called_once_with(mock_page, 100, 200)
        mock_page.mouse.down.assert_called_once()
        mock_page.mouse.up.assert_called_once()
