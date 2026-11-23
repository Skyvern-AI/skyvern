"""``drag``/``left_mouse`` must honour a 0 coordinate as a real position.

Both helpers type their coordinates as ``int | None`` and treat ``None`` as "no position given"
(``drag`` already does that for its ``last_x``/``last_y``). Gating the leading ``move_cursor`` on
truthiness instead made a start point on the viewport's left or top edge look unset, so the mouse
pressed wherever the cursor happened to be.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.actions import handler_utils


def _page() -> MagicMock:
    page = MagicMock()
    page.mouse.down = AsyncMock()
    page.mouse.up = AsyncMock()
    return page


@pytest.mark.asyncio
async def test_zero_coordinate_moves_the_cursor_and_an_absent_one_does_not() -> None:
    with patch.object(handler_utils.EventStrategyFactory, "move_cursor", new=AsyncMock()) as move_cursor:

        def moves() -> list[tuple[int, int]]:
            return [call.args[1:] for call in move_cursor.await_args_list]

        await handler_utils.drag(_page(), start_x=0, start_y=40, path=[(120, 40)])
        assert moves() == [(0, 40), (120, 40)]

        move_cursor.reset_mock()
        await handler_utils.drag(_page(), path=[(120, 40)])
        assert moves() == [(120, 40)]

        move_cursor.reset_mock()
        await handler_utils.left_mouse(_page(), 0, 25, "down")
        assert moves() == [(0, 25)]

        move_cursor.reset_mock()
        await handler_utils.left_mouse(_page(), None, None, "up")
        assert moves() == []
