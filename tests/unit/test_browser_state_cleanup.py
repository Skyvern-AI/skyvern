from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.real_browser_state import RealBrowserState


@pytest.fixture
def browser_state():
    pw = MagicMock()
    pw.stop = AsyncMock()
    ctx = MagicMock()
    ctx.close = AsyncMock()
    cleanup = AsyncMock()
    state = RealBrowserState(
        pw=pw,
        browser_context=ctx,
        browser_cleanup=cleanup,
    )
    return state, pw, ctx, cleanup


class TestBrowserStateClose:
    @pytest.mark.asyncio
    async def test_close_true_runs_context_close_and_cleanup(self, browser_state):
        state, pw, ctx, cleanup = browser_state
        await state.close(close_browser_on_completion=True)
        ctx.close.assert_called_once()
        cleanup.assert_called_once()
        pw.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_false_still_runs_cleanup(self, browser_state):
        state, pw, ctx, cleanup = browser_state
        await state.close(close_browser_on_completion=False)
        ctx.close.assert_not_called()
        cleanup.assert_called_once()
        pw.stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_skip_cleanup(self, browser_state):
        state, pw, ctx, cleanup = browser_state
        await state.close(close_browser_on_completion=False, skip_cleanup=True)
        ctx.close.assert_not_called()
        cleanup.assert_not_called()
        pw.stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_true_skip_cleanup(self, browser_state):
        state, pw, ctx, cleanup = browser_state
        await state.close(close_browser_on_completion=True, skip_cleanup=True)
        ctx.close.assert_called_once()
        cleanup.assert_not_called()
        pw.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_no_cleanup_func_is_noop(self):
        pw = MagicMock()
        pw.stop = AsyncMock()
        ctx = MagicMock()
        ctx.close = AsyncMock()
        state = RealBrowserState(pw=pw, browser_context=ctx, browser_cleanup=None)
        await state.close(close_browser_on_completion=False)
        ctx.close.assert_not_called()
        pw.stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_raise(self, browser_state):
        state, pw, ctx, cleanup = browser_state
        cleanup.side_effect = RuntimeError("cleanup failed")
        await state.close(close_browser_on_completion=False)
        cleanup.assert_called_once()
