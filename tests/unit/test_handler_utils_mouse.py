from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.actions import handler_utils


def _mock_page() -> SimpleNamespace:
    return SimpleNamespace(mouse=SimpleNamespace(down=AsyncMock(), up=AsyncMock()))


@pytest.mark.asyncio
async def test_drag_moves_cursor_to_start_when_coordinate_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _mock_page()
    move_cursor = AsyncMock()
    sync_cursor_position = MagicMock()
    monkeypatch.setattr(handler_utils.EventStrategyFactory, "move_cursor", move_cursor)
    monkeypatch.setattr(handler_utils.EventStrategyFactory, "sync_cursor_position", sync_cursor_position)

    await handler_utils.drag(page, start_x=0, start_y=5)

    move_cursor.assert_awaited_once_with(page, 0, 5)
    page.mouse.down.assert_awaited_once()
    page.mouse.up.assert_awaited_once()
    sync_cursor_position.assert_called_once_with(page, 0, 5)


@pytest.mark.asyncio
async def test_left_mouse_moves_cursor_when_coordinate_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _mock_page()
    move_cursor = AsyncMock()
    monkeypatch.setattr(handler_utils.EventStrategyFactory, "move_cursor", move_cursor)

    await handler_utils.left_mouse(page, 0, 0, "down")

    move_cursor.assert_awaited_once_with(page, 0, 0)
    page.mouse.down.assert_awaited_once()
    page.mouse.up.assert_not_awaited()
