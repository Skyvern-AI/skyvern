"""Tests for the GOTO_URL / RELOAD_PAGE action handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.webeye.actions import actions
from skyvern.webeye.actions.handler import handle_goto_url_action, handle_reload_page_action
from skyvern.webeye.actions.responses import ActionSuccess


def _task() -> MagicMock:
    task = MagicMock()
    task.task_id = "tsk_test"
    task.workflow_run_id = None
    return task


@pytest.mark.asyncio
async def test_goto_url_navigates_and_stops_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    validate_url = MagicMock(return_value="https://example.test/page")
    monkeypatch.setattr("skyvern.webeye.actions.handler.validate_fetch_url", validate_url)
    page = MagicMock()
    page.goto = AsyncMock()

    action = actions.GotoUrlAction(url="https://example.test/page")
    result = await handle_goto_url_action(action, page, MagicMock(), _task(), MagicMock())

    assert len(result) == 1
    assert isinstance(result[0], ActionSuccess)
    # Navigation invalidates pre-nav element ids, so later actions in the batch must not run.
    assert result[0].skip_remaining_actions is True
    page.goto.assert_awaited_once()
    validate_url.assert_called_once_with("https://example.test/page")


@pytest.mark.asyncio
async def test_reload_page_reloads_and_stops_batch() -> None:
    page = MagicMock()
    page.reload = AsyncMock()

    action = actions.ReloadPageAction()
    result = await handle_reload_page_action(action, page, MagicMock(), _task(), MagicMock())

    assert len(result) == 1
    assert isinstance(result[0], ActionSuccess)
    assert result[0].skip_remaining_actions is True
    page.reload.assert_awaited_once()


_METADATA_HOP = "http://169.254.169.254/latest/meta-data/"


def _redirect_chain(*urls: str) -> SimpleNamespace:
    """page.goto-style response whose followed redirect chain visited ``urls`` in order."""
    request: SimpleNamespace | None = None
    for url in urls:
        request = SimpleNamespace(url=url, redirected_from=request)
    return SimpleNamespace(request=request)


def _refuse_metadata(url: str) -> str:
    if "169.254.169.254" in url:
        raise BlockedHost(host=url)
    return url


@pytest.mark.asyncio
async def test_goto_url_refuses_a_blocked_redirect_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.webeye.actions.handler.validate_fetch_url", _refuse_metadata)
    page = MagicMock()
    page.goto = AsyncMock(return_value=_redirect_chain("https://example.test/page", _METADATA_HOP))

    action = actions.GotoUrlAction(url="https://example.test/page")

    with pytest.raises(BlockedHost):
        await handle_goto_url_action(action, page, MagicMock(), _task(), MagicMock())

    assert page.goto.await_args_list == [
        call("https://example.test/page", timeout=settings.BROWSER_LOADING_TIMEOUT_MS),
        call("about:blank"),
    ]


@pytest.mark.asyncio
async def test_goto_url_allows_a_public_redirect_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.webeye.actions.handler.validate_fetch_url", _refuse_metadata)
    page = MagicMock()
    page.goto = AsyncMock(return_value=_redirect_chain("https://example.test/page", "https://cdn.example.test/final"))

    action = actions.GotoUrlAction(url="https://example.test/page")
    result = await handle_goto_url_action(action, page, MagicMock(), _task(), MagicMock())

    assert isinstance(result[0], ActionSuccess)
    page.goto.assert_awaited_once_with(
        "https://example.test/page",
        timeout=settings.BROWSER_LOADING_TIMEOUT_MS,
    )
