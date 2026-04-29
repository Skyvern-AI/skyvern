"""Tests for AgentFunction._maybe_close_magic_link_page fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.models import StepStatus


def _make_step(*, status: StepStatus = StepStatus.completed) -> MagicMock:
    step = MagicMock()
    step.status = status
    return step


def _make_task(task_id: str = "tsk_test") -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    return task


def _make_page(*, closed: bool = False, inner_text: str = "") -> MagicMock:
    page = MagicMock()
    page.is_closed.return_value = closed
    page.url = "https://example.com/confirmation"
    page.inner_text = AsyncMock(return_value=inner_text)
    page.close = AsyncMock()
    return page


def _make_context(task_id: str, page: MagicMock | None, *, has_page: bool | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.magic_link_pages = {task_id: page} if page else {}
    # Default: has_page = True if page is provided and not closed
    if has_page is None:
        has_page = page is not None and not page.is_closed()
    ctx.has_magic_link_page.return_value = has_page
    return ctx


@pytest.mark.asyncio
async def test_no_context() -> None:
    """Should return silently when no SkyvernContext exists."""
    agent_fn = AgentFunction()
    with patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx:
        mock_ctx.current.return_value = None
        await agent_fn._maybe_close_magic_link_page(_make_task())
    # No exception raised


@pytest.mark.asyncio
async def test_no_magic_link_page() -> None:
    """Should return silently when no magic link page is tracked for this task."""
    agent_fn = AgentFunction()
    ctx = _make_context("tsk_test", None)
    with patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx:
        mock_ctx.current.return_value = ctx
        await agent_fn._maybe_close_magic_link_page(_make_task())


@pytest.mark.asyncio
async def test_page_already_closed() -> None:
    """Should return early when has_magic_link_page returns False (page already closed)."""
    agent_fn = AgentFunction()
    page = _make_page(closed=True)
    ctx = _make_context("tsk_test", page)  # has_page defaults to False for closed pages
    with patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx:
        mock_ctx.current.return_value = ctx
        await agent_fn._maybe_close_magic_link_page(_make_task())
    page.close.assert_not_called()


@pytest.mark.asyncio
async def test_no_close_signal() -> None:
    """Should keep the page open when no close signal is found."""
    agent_fn = AgentFunction()
    page = _make_page(inner_text="Welcome to the dashboard. Here are your settings.")
    ctx = _make_context("tsk_test", page)
    with patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx:
        mock_ctx.current.return_value = ctx
        await agent_fn._maybe_close_magic_link_page(_make_task())
    assert "tsk_test" in ctx.magic_link_pages
    page.close.assert_not_called()


@pytest.mark.asyncio
async def test_close_signal_matched() -> None:
    """Should close the page and clean up context when a signal matches."""
    agent_fn = AgentFunction()
    page = _make_page(
        inner_text="Account verification successful. You can close this page and return to the original page."
    )
    ctx = _make_context("tsk_test", page)
    with patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx:
        mock_ctx.current.return_value = ctx
        await agent_fn._maybe_close_magic_link_page(_make_task())
    assert "tsk_test" not in ctx.magic_link_pages
    page.close.assert_called_once()


@pytest.mark.asyncio
async def test_close_signal_case_insensitive() -> None:
    """Signal matching should be case-insensitive."""
    agent_fn = AgentFunction()
    page = _make_page(inner_text="You May Now Close This Tab")
    ctx = _make_context("tsk_test", page)
    with patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx:
        mock_ctx.current.return_value = ctx
        await agent_fn._maybe_close_magic_link_page(_make_task())
    assert "tsk_test" not in ctx.magic_link_pages
    page.close.assert_called_once()


@pytest.mark.asyncio
async def test_inner_text_failure() -> None:
    """Should skip auto-close when reading page text fails."""
    agent_fn = AgentFunction()
    page = _make_page()
    page.inner_text = AsyncMock(side_effect=Exception("page crashed"))
    ctx = _make_context("tsk_test", page)
    with patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx:
        mock_ctx.current.return_value = ctx
        await agent_fn._maybe_close_magic_link_page(_make_task())
    assert "tsk_test" in ctx.magic_link_pages
    page.close.assert_not_called()


@pytest.mark.asyncio
async def test_page_close_failure_keeps_reference() -> None:
    """Should keep the stale reference for retry when page.close() fails."""
    agent_fn = AgentFunction()
    page = _make_page(inner_text="You can close this page now.")
    page.close = AsyncMock(side_effect=Exception("close failed"))
    ctx = _make_context("tsk_test", page)
    with patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx:
        mock_ctx.current.return_value = ctx
        await agent_fn._maybe_close_magic_link_page(_make_task())
    # Reference kept for retry on next step
    assert "tsk_test" in ctx.magic_link_pages


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [StepStatus.failed, StepStatus.created])
async def test_skipped_on_non_completed_step(status: StepStatus) -> None:
    """Should skip magic link check when step is not completed."""
    agent_fn = AgentFunction()
    page = _make_page(inner_text="You can close this page now.")
    ctx = _make_context("tsk_test", page)
    with patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx:
        mock_ctx.current.return_value = ctx
        await agent_fn.post_step_execution(_make_task(), _make_step(status=status))
    # Page should NOT be closed — step didn't complete
    assert "tsk_test" in ctx.magic_link_pages
    page.close.assert_not_called()
