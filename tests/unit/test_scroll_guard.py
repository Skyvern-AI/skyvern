"""Tests for scroll_to_top and scroll_to_next_page None-guard in SkyvernFrame."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from skyvern.webeye.utils.page import SkyvernFrame


@pytest.fixture
def skyvern_frame() -> SkyvernFrame:
    """Create a SkyvernFrame with a mock frame."""
    frame = AsyncMock()
    sf = SkyvernFrame.__new__(SkyvernFrame)
    sf.frame = frame
    return sf


@pytest.mark.asyncio
async def test_scroll_to_top_returns_float(skyvern_frame: SkyvernFrame) -> None:
    with patch.object(SkyvernFrame, "evaluate", new_callable=AsyncMock, return_value=123.45):
        result = await skyvern_frame.scroll_to_top(draw_boxes=False, frame="", frame_index=0)
    assert result == 123.45
    assert isinstance(result, float)


@pytest.mark.asyncio
async def test_scroll_to_top_returns_float_from_int(skyvern_frame: SkyvernFrame) -> None:
    with patch.object(SkyvernFrame, "evaluate", new_callable=AsyncMock, return_value=0):
        result = await skyvern_frame.scroll_to_top(draw_boxes=False, frame="", frame_index=0)
    assert result == 0.0
    assert isinstance(result, float)


@pytest.mark.asyncio
async def test_scroll_to_top_none_returns_zero(skyvern_frame: SkyvernFrame) -> None:
    with patch.object(SkyvernFrame, "evaluate", new_callable=AsyncMock, return_value=None):
        result = await skyvern_frame.scroll_to_top(draw_boxes=False, frame="", frame_index=0)
    assert result == 0.0
    assert isinstance(result, float)


@pytest.mark.asyncio
async def test_scroll_to_top_string_returns_zero(skyvern_frame: SkyvernFrame) -> None:
    with patch.object(SkyvernFrame, "evaluate", new_callable=AsyncMock, return_value="bad"):
        result = await skyvern_frame.scroll_to_top(draw_boxes=False, frame="", frame_index=0)
    assert result == 0.0


@pytest.mark.asyncio
async def test_scroll_to_next_page_returns_float(skyvern_frame: SkyvernFrame) -> None:
    with patch.object(SkyvernFrame, "evaluate", new_callable=AsyncMock, return_value=500.0):
        result = await skyvern_frame.scroll_to_next_page(draw_boxes=False, frame="", frame_index=0)
    assert result == 500.0
    assert isinstance(result, float)


@pytest.mark.asyncio
async def test_scroll_to_next_page_none_returns_zero(skyvern_frame: SkyvernFrame) -> None:
    with patch.object(SkyvernFrame, "evaluate", new_callable=AsyncMock, return_value=None):
        result = await skyvern_frame.scroll_to_next_page(draw_boxes=False, frame="", frame_index=0)
    assert result == 0.0
    assert isinstance(result, float)


@pytest.mark.asyncio
async def test_scroll_to_next_page_string_returns_zero(skyvern_frame: SkyvernFrame) -> None:
    with patch.object(SkyvernFrame, "evaluate", new_callable=AsyncMock, return_value="bad"):
        result = await skyvern_frame.scroll_to_next_page(draw_boxes=False, frame="", frame_index=0)
    assert result == 0.0
