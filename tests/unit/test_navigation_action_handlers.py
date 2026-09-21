"""Tests for the GOTO_URL / RELOAD_PAGE action handlers."""

from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, call

import pytest

import skyvern.webeye.actions.handler as handler_module
import skyvern.webeye.navigation as navigation_module
from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
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


@pytest.mark.asyncio
async def test_a_failed_goto_action_records_the_drivers_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """These actions call the driver directly, so their failure becomes an ActionFailure and never
    reaches the typed navigation error. Without capturing here the code is gone by the time anything
    decides who owned the failure, and an egress fault reads as a defect in the run.

    The requested destination is excluded: it is chosen by whoever asked for the navigation, so a
    code spelled inside it is not the browser's verdict.
    """
    planted = "https://x.test/net::ERR_TUNNEL_CONNECTION_FAILED"
    monkeypatch.setattr(handler_module, "validate_fetch_url", lambda url: url)
    # Corroboration does a real DNS lookup by default; this test is about destination exclusion, not
    # about the .test fixture host resolving, so pin it to "has an address record".
    monkeypatch.setattr(navigation_module, "host_has_no_address_record", lambda host: False)
    page = SimpleNamespace(
        goto=AsyncMock(side_effect=RuntimeError(f"Page.goto: net::ERR_NAME_NOT_RESOLVED at {planted}"))
    )
    task = _task()
    task.task_id = "tsk_nav"
    context = SkyvernContext(run_id="wr_nav")

    with mock.patch.object(skyvern_context, "current", return_value=context):
        with pytest.raises(RuntimeError):
            await handle_goto_url_action(actions.GotoUrlAction(url=planted), page, MagicMock(), task, MagicMock())

    assert context.task_nav_error_codes == {"tsk_nav": "net::ERR_NAME_NOT_RESOLVED"}


@pytest.mark.asyncio
async def test_a_successful_retry_drops_the_earlier_navigation_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry that succeeds leaves the failure behind it.

    Keeping the code would let a later failure of a different kind inherit it and be reported as a
    network fault -- the same staleness the keyed carrier exists to avoid.
    """
    monkeypatch.setattr(handler_module, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(handler_module, "revalidate_redirect_chain", AsyncMock())
    task = _task()
    task.task_id = "tsk_retry"
    context = SkyvernContext(run_id="wr_retry")
    context.task_nav_error_codes["tsk_retry"] = "net::ERR_NAME_NOT_RESOLVED"
    page = SimpleNamespace(goto=AsyncMock(return_value=None))

    with mock.patch.object(skyvern_context, "current", return_value=context):
        await handle_goto_url_action(
            actions.GotoUrlAction(url="https://x.test/ok"), page, MagicMock(), task, MagicMock()
        )

    assert context.task_nav_error_codes == {}


@pytest.mark.asyncio
async def test_a_failed_reload_records_the_drivers_code() -> None:
    """Reload navigates too, and its failure becomes an ActionFailure like the others."""
    task = _task()
    task.task_id = "tsk_reload"
    context = SkyvernContext(run_id="wr_reload")
    page = SimpleNamespace(
        url="https://x.test/catalogue",
        reload=AsyncMock(side_effect=RuntimeError("Page.reload: net::ERR_CERT_DATE_INVALID")),
    )

    with mock.patch.object(skyvern_context, "current", return_value=context):
        with pytest.raises(RuntimeError):
            await handle_reload_page_action(actions.ReloadPageAction(), page, MagicMock(), task, MagicMock())

    assert context.task_nav_error_codes == {"tsk_reload": "net::ERR_CERT_DATE_INVALID"}


@pytest.mark.asyncio
async def test_a_later_failure_without_a_code_is_not_judged_on_the_earlier_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that fails differently still leaves the first attempt's verdict behind.

    Clearing only on success is not enough: a timeout carries no net:: token, so the tunnel code
    from the attempt before it would be attached to this failure and blamed on our egress.
    """
    monkeypatch.setattr(handler_module, "validate_fetch_url", lambda url: url)
    task = _task()
    task.task_id = "tsk_second"
    context = SkyvernContext(run_id="wr_second")
    context.task_nav_error_codes["tsk_second"] = "net::ERR_TUNNEL_CONNECTION_FAILED"
    page = SimpleNamespace(goto=AsyncMock(side_effect=RuntimeError("Page.goto: Timeout 30000ms exceeded")))

    with mock.patch.object(skyvern_context, "current", return_value=context):
        with pytest.raises(RuntimeError):
            await handle_goto_url_action(
                actions.GotoUrlAction(url="https://x.test/slow"), page, MagicMock(), task, MagicMock()
            )

    assert context.task_nav_error_codes == {}


@pytest.mark.asyncio
async def test_a_later_action_retires_the_navigation_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task can fail a navigation, keep going, and end on something else entirely.

    The code describes the navigation that recorded it, so it must not be attached to whatever
    failure the task finally reports -- an unrelated click failure blamed on our egress suppresses
    repair of the block that actually broke.
    """
    task = _task()
    task.task_id = "tsk_later"
    context = SkyvernContext(run_id="wr_later")
    context.task_nav_error_codes["tsk_later"] = "net::ERR_TUNNEL_CONNECTION_FAILED"

    monkeypatch.setattr(handler_module, "preflight_action", lambda *a, **k: None)
    monkeypatch.setattr(handler_module.app.BROWSER_MANAGER, "get_for_task", MagicMock(return_value=None), raising=False)

    with mock.patch.object(skyvern_context, "current", return_value=context):
        await handler_module.ActionHandler.handle_action(
            MagicMock(), task, MagicMock(order=1), MagicMock(), MagicMock()
        )

    assert context.task_nav_error_codes == {}


@pytest.mark.asyncio
async def test_terminating_after_a_failed_navigation_keeps_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminate and complete dispatch through handle_action like any other action.

    They are how a task ends, so retiring the code on them erases it in exactly the case this change
    exists for: the page would not load, so the agent gave up.
    """
    task = _task()
    task.task_id = "tsk_terminate"
    context = SkyvernContext(run_id="wr_terminate")
    context.task_nav_error_codes["tsk_terminate"] = "net::ERR_TUNNEL_CONNECTION_FAILED"

    monkeypatch.setattr(handler_module, "preflight_action", lambda *a, **k: None)
    monkeypatch.setattr(handler_module.app.BROWSER_MANAGER, "get_for_task", MagicMock(return_value=None), raising=False)
    terminate = MagicMock()
    terminate.action_type = handler_module.ActionType.TERMINATE

    with mock.patch.object(skyvern_context, "current", return_value=context):
        await handler_module.ActionHandler.handle_action(MagicMock(), task, MagicMock(order=2), MagicMock(), terminate)

    assert context.task_nav_error_codes == {"tsk_terminate": "net::ERR_TUNNEL_CONNECTION_FAILED"}
