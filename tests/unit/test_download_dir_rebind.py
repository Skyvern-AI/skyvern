"""Run-scoped download dir binding for adopted persistent sessions (SKY-11083).

A persistent browser session launched outside any run context binds its CDP
downloadPath to downloads/None/. When a workflow run later adopts that session,
the dir must be rebound to downloads/<workflow_run_id>/ so downloads land
run-scoped and the listener logs the real run identity.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from structlog.testing import capture_logs

from skyvern.forge.sdk.api.files import get_download_dir, resolve_run_download_id
from skyvern.forge.sdk.core.http_request_authorization import RunScopedRedirectHopAuthorizer
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.block import Block, CodeBlock, PrintPageBlock
from skyvern.webeye.browser_artifacts import DownloadBinding
from skyvern.webeye.browser_factory import (
    _apply_download_behaviour,
    _create_headless_chromium,
    rebind_download_dir,
    set_download_file_listener,
)
from skyvern.webeye.real_browser_manager import RealBrowserManager


@pytest_asyncio.fixture
async def browser_manager() -> AsyncIterator[RealBrowserManager]:
    manager = RealBrowserManager()
    try:
        yield manager
    finally:
        # Adoption starts a lease-renewal task even with a mocked browser.
        # Join this test's task before pytest closes its event loop.
        renewer = manager._session_activity_renewer
        if renewer is not None:
            renewer.cancel()
            with suppress(asyncio.CancelledError):
                await renewer


def _recording_browser() -> tuple[MagicMock, MagicMock]:
    cdp_session = MagicMock()
    cdp_session.send = AsyncMock()
    browser = MagicMock()
    browser.new_browser_cdp_session = AsyncMock(return_value=cdp_session)
    browser.contexts = []
    return browser, cdp_session


def _recording_context_page() -> tuple[MagicMock, MagicMock, MagicMock]:
    cdp_session = MagicMock()
    cdp_session.send = AsyncMock()
    context = MagicMock()
    context.new_cdp_session = AsyncMock(return_value=cdp_session)
    context._skyvern_cdp_download_interceptor = None
    page = MagicMock()
    page.context = context
    return page, context, cdp_session


def _workflow_attach_run() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_block",
        parent_workflow_run_id=None,
        workflow_permanent_id="wpid_block",
        organization_id="org_1",
        browser_profile_id=None,
        proxy_location=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
    )


def _assert_scope_rebound(interceptor: MagicMock, run_id: str) -> None:
    interceptor.rebind_download_scope.assert_called_once()
    kwargs = interceptor.rebind_download_scope.call_args.kwargs
    assert kwargs["download_dir"] == get_download_dir(run_id)
    authorizer = kwargs["redirect_hop_authorizer"]
    assert isinstance(authorizer, RunScopedRedirectHopAuthorizer)
    assert authorizer.download_scope == run_id


@pytest.mark.asyncio
async def test_rebind_binds_download_path_to_run_dir() -> None:
    browser, cdp_session = _recording_browser()

    await rebind_download_dir(browser, run_id="wr_test")

    cdp_session.send.assert_awaited_once()
    method, params = cdp_session.send.await_args.args
    assert method == "Browser.setDownloadBehavior"
    assert params["downloadPath"] == get_download_dir("wr_test")
    assert "None" not in params["downloadPath"]


@pytest.mark.asyncio
async def test_rebind_skips_when_run_id_none() -> None:
    browser, cdp_session = _recording_browser()

    await rebind_download_dir(browser, run_id=None)

    cdp_session.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebind_also_rebinds_cdp_download_interceptor() -> None:
    browser, cdp_session = _recording_browser()
    interceptor = MagicMock()
    interceptor.is_monitoring_browser_downloads = MagicMock(return_value=False)
    context = MagicMock()
    context._skyvern_cdp_download_interceptor = interceptor
    browser.contexts = [context]

    await rebind_download_dir(browser, run_id="wr_test")

    _assert_scope_rebound(interceptor, "wr_test")


@pytest.mark.asyncio
async def test_rebind_waits_for_context_ownership_lock() -> None:
    browser, _cdp_session = _recording_browser()
    interceptor = MagicMock()
    interceptor.is_monitoring_browser_downloads = MagicMock(return_value=True)

    @asynccontextmanager
    async def settled():
        yield

    interceptor.settle_browser_downloads = settled
    context = MagicMock()
    context._skyvern_cdp_download_interceptor = interceptor
    context._skyvern_cdp_download_interceptor_bind_lock = asyncio.Lock()
    browser.contexts = [context]

    await context._skyvern_cdp_download_interceptor_bind_lock.acquire()
    rebinding = asyncio.create_task(rebind_download_dir(browser, run_id="wr_test"))
    await asyncio.sleep(0)
    interceptor.rebind_download_scope.assert_not_called()
    context._skyvern_cdp_download_interceptor_bind_lock.release()
    await asyncio.wait_for(rebinding, timeout=1)

    _assert_scope_rebound(interceptor, "wr_test")


@pytest.mark.asyncio
async def test_rebind_settlement_timeout_fails_before_scope_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    browser, _cdp_session = _recording_browser()
    interceptor = MagicMock()

    @asynccontextmanager
    async def never_settles():
        await asyncio.Event().wait()
        yield

    interceptor.settle_browser_downloads = never_settles
    context = MagicMock()
    context._skyvern_cdp_download_interceptor = interceptor
    context._skyvern_cdp_download_interceptor_bind_lock = asyncio.Lock()
    context._skyvern_download_run_id = "prior_run"
    browser.contexts = [context]
    monkeypatch.setattr("skyvern.webeye.browser_factory.SAVE_DOWNLOADED_FILES_TIMEOUT", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(rebind_download_dir(browser, run_id="wr_test"), timeout=1)

    interceptor.rebind_download_scope.assert_not_called()
    interceptor.invalidate_download_scope.assert_called_once()
    assert context._skyvern_download_run_id is None


@pytest.mark.asyncio
async def test_rebind_rotation_failure_revokes_prior_scope() -> None:
    browser, _cdp_session = _recording_browser()
    interceptor = MagicMock()
    interceptor.rebind_download_scope.side_effect = OSError("directory unavailable")
    context = MagicMock()
    context._skyvern_cdp_download_interceptor = interceptor
    context._skyvern_cdp_download_interceptor_bind_lock = asyncio.Lock()
    context._skyvern_download_run_id = "prior_run"
    browser.contexts = [context]

    with pytest.raises(OSError, match="directory unavailable"):
        await rebind_download_dir(browser, run_id="wr_test")

    interceptor.invalidate_download_scope.assert_called_once()
    assert context._skyvern_download_run_id is None


@pytest.mark.asyncio
async def test_cancelled_rebind_waiter_does_not_revoke_current_lock_owner() -> None:
    browser, _cdp_session = _recording_browser()
    interceptor = MagicMock()
    bind_lock = asyncio.Lock()
    await bind_lock.acquire()
    context = MagicMock()
    context._skyvern_cdp_download_interceptor = interceptor
    context._skyvern_cdp_download_interceptor_bind_lock = bind_lock
    context._skyvern_download_run_id = "prior_run"
    browser.contexts = [context]

    rebinding = asyncio.create_task(rebind_download_dir(browser, run_id="wr_test"))
    await asyncio.sleep(0)
    rebinding.cancel()
    with pytest.raises(asyncio.CancelledError):
        await rebinding
    bind_lock.release()

    interceptor.invalidate_download_scope.assert_not_called()
    assert context._skyvern_download_run_id == "prior_run"


@pytest.mark.asyncio
async def test_rebind_ignores_context_without_interceptor() -> None:
    browser, cdp_session = _recording_browser()
    browser.contexts = [SimpleNamespace()]

    await rebind_download_dir(browser, run_id="wr_test")

    cdp_session.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_rebind_never_detaches_the_session_that_installed_the_binding() -> None:
    # Chromium scopes setDownloadBehavior to the installing session and reverts the binding when
    # that session detaches -- a detach after the rebind silently redirects every later download
    # away from the run's directory, measured on both engines (#15207 review).
    browser, browser_session = _recording_browser()
    await rebind_download_dir(browser, run_id="wr_test")
    browser_session.detach.assert_not_called()

    page, _, page_session = _recording_context_page()
    await rebind_download_dir(None, run_id="wr_test", page=page)
    page_session.detach.assert_not_called()


@pytest.mark.asyncio
async def test_rebind_uses_context_cdp_session_without_owning_browser() -> None:
    page, context, cdp_session = _recording_context_page()

    await rebind_download_dir(None, run_id="wr_test", page=page)

    context.new_cdp_session.assert_awaited_once_with(page)
    method, params = cdp_session.send.await_args.args
    assert method == "Browser.setDownloadBehavior"
    assert params["downloadPath"] == get_download_dir("wr_test")
    assert "None" not in params["downloadPath"]


@pytest.mark.asyncio
async def test_rebind_context_path_rebinds_supplied_context_interceptor() -> None:
    page, context, _cdp_session = _recording_context_page()
    interceptor = MagicMock()
    interceptor.is_monitoring_browser_downloads = MagicMock(return_value=False)
    context._skyvern_cdp_download_interceptor = interceptor

    await rebind_download_dir(None, run_id="wr_test", page=page)

    _assert_scope_rebound(interceptor, "wr_test")


@pytest.mark.asyncio
async def test_rebind_context_path_without_interceptor_still_sets_behaviour() -> None:
    page, context, cdp_session = _recording_context_page()
    context._skyvern_cdp_download_interceptor = None

    await rebind_download_dir(None, run_id="wr_test", page=page)

    cdp_session.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_rebind_no_browser_and_no_page_is_noop() -> None:
    await rebind_download_dir(None, run_id="wr_test")


@pytest.mark.asyncio
async def test_rebind_prefers_owning_browser_over_page() -> None:
    browser, cdp_session = _recording_browser()
    page, context, _context_cdp = _recording_context_page()

    await rebind_download_dir(browser, run_id="wr_test", page=page)

    browser.new_browser_cdp_session.assert_awaited_once()
    context.new_cdp_session.assert_not_awaited()
    cdp_session.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_rebind_still_rebinds_interceptor_when_setdownloadbehavior_raises() -> None:
    browser, cdp_session = _recording_browser()
    cdp_session.send = AsyncMock(side_effect=RuntimeError("method not found"))
    interceptor = MagicMock()
    interceptor.is_monitoring_browser_downloads = MagicMock(return_value=False)
    context = MagicMock()
    context._skyvern_cdp_download_interceptor = interceptor
    browser.contexts = [context]

    await rebind_download_dir(browser, run_id="wr_test")

    _assert_scope_rebound(interceptor, "wr_test")


@pytest.mark.asyncio
async def test_rebind_does_not_raise_on_launch_path_without_interceptor() -> None:
    """Fail-open: a setDownloadBehavior failure on the launch path (browser, no
    interceptor) must never propagate and break the browser launch."""
    browser, cdp_session = _recording_browser()
    cdp_session.send = AsyncMock(side_effect=RuntimeError("method not found"))
    browser.contexts = []

    await rebind_download_dir(browser, run_id="wr_test")


@pytest.mark.asyncio
async def test_apply_download_behaviour_swallows_setdownloadbehavior_failure() -> None:
    """The launch caller (_apply_download_behaviour) must not raise when the rebind CDP call fails."""
    browser, cdp_session = _recording_browser()
    cdp_session.send = AsyncMock(side_effect=RuntimeError("method not found"))
    browser.contexts = []
    ctx = SkyvernContext(run_id="run_x", workflow_run_id="wr_y")

    with patch("skyvern.webeye.browser_factory.ensure_context", return_value=ctx):
        await _apply_download_behaviour(browser)


@pytest.mark.asyncio
async def test_rebind_does_not_downgrade_active_download_monitor() -> None:
    """When a download monitor owns the context binding ({deny, eventsEnabled:True}),
    rebind only its dir and never re-send setDownloadBehavior allow, which would disable it."""
    browser, cdp_session = _recording_browser()
    interceptor = MagicMock()
    interceptor.is_monitoring_browser_downloads = MagicMock(return_value=True)
    context = MagicMock()
    context._skyvern_cdp_download_interceptor = interceptor
    browser.contexts = [context]

    await rebind_download_dir(browser, run_id="wr_test")

    _assert_scope_rebound(interceptor, "wr_test")
    cdp_session.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebind_sends_allow_when_interceptor_monitor_inactive() -> None:
    """An interceptor that is not monitoring (no browser session) does not own the binding, so the
    allow/downloadPath rebind still fires for the local path."""
    browser, cdp_session = _recording_browser()
    interceptor = MagicMock()
    interceptor.is_monitoring_browser_downloads = MagicMock(return_value=False)
    context = MagicMock()
    context._skyvern_cdp_download_interceptor = interceptor
    browser.contexts = [context]

    await rebind_download_dir(browser, run_id="wr_test")

    _assert_scope_rebound(interceptor, "wr_test")
    cdp_session.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_download_behaviour_uses_run_id_first_resolution() -> None:
    browser, cdp_session = _recording_browser()
    ctx = SkyvernContext(run_id="run_x", workflow_run_id="wr_y", task_id="t_z")

    with patch("skyvern.webeye.browser_factory.ensure_context", return_value=ctx):
        await _apply_download_behaviour(browser)

    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"] == get_download_dir("run_x")


@pytest.mark.asyncio
async def test_apply_download_behaviour_falls_back_to_workflow_run_id() -> None:
    browser, cdp_session = _recording_browser()
    ctx = SkyvernContext(run_id=None, workflow_run_id="wr_y", task_id="t_z")

    with patch("skyvern.webeye.browser_factory.ensure_context", return_value=ctx):
        await _apply_download_behaviour(browser)

    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"] == get_download_dir("wr_y")


@pytest.mark.asyncio
async def test_connect_creator_skips_run_dir_setdownloadbehavior_for_session_dir() -> None:
    """The generic ``browser_address`` reconnect path must not re-point a provider-owned remote
    binding's downloads to the run dir. ``_connect_to_cdp_browser`` skips ``_apply_download_behaviour``
    for SESSION_DIR so the provider-selected destination is preserved, and stamps the fresh artifacts
    SESSION_DIR by construction (not a later relabel)."""
    browser, cdp_session = _recording_browser()
    browser.new_context = AsyncMock(return_value=MagicMock(pages=[]))
    ctx = SkyvernContext(run_id="run_x", workflow_run_id="wr_y")

    with (
        patch("skyvern.webeye.browser_factory._connect_over_cdp_with_diagnostics", AsyncMock(return_value=browser)),
        patch("skyvern.webeye.browser_factory.ensure_context", return_value=ctx),
    ):
        _, artifacts, _ = await _create_headless_chromium(
            MagicMock(),
            browser_address="ws://remote.example/cdp",
            download_binding=DownloadBinding.SESSION_DIR,
        )

    sent_methods = [call.args[0] for call in cdp_session.send.await_args_list if call.args]
    assert "Browser.setDownloadBehavior" not in sent_methods
    assert artifacts.download_binding == DownloadBinding.SESSION_DIR


@pytest.mark.asyncio
async def test_connect_creator_binds_run_dir_setdownloadbehavior_for_run_dir() -> None:
    """RUN_DIR (default local/OSS/vendor) still physically binds downloads to the run dir on connect —
    the SESSION_DIR skip must not disable the ordinary run-scoped rebind."""
    browser, cdp_session = _recording_browser()
    browser.new_context = AsyncMock(return_value=MagicMock(pages=[]))
    ctx = SkyvernContext(run_id="run_x", workflow_run_id="wr_y")

    with (
        patch("skyvern.webeye.browser_factory._connect_over_cdp_with_diagnostics", AsyncMock(return_value=browser)),
        patch("skyvern.webeye.browser_factory.ensure_context", return_value=ctx),
    ):
        _, artifacts, _ = await _create_headless_chromium(
            MagicMock(),
            browser_address="ws://remote.example/cdp",
        )

    method, params = cdp_session.send.await_args.args
    assert method == "Browser.setDownloadBehavior"
    assert params["downloadPath"] == get_download_dir("run_x")
    assert artifacts.download_binding == DownloadBinding.RUN_DIR


@pytest.mark.asyncio
async def test_listener_logs_run_identity_from_context(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def capture_log(_msg: str, **kwargs: object) -> None:
        captured.update(kwargs)

    extensionless_file = tmp_path / "statement"
    extensionless_file.write_bytes(b"data")
    download = MagicMock()
    download.suggested_filename = "statement"
    download.url = "https://example.com/d"
    download.path = AsyncMock(return_value=extensionless_file)

    captured_handler: dict[str, object] = {}

    def on_page(_event: str, handler: object) -> None:
        captured_handler["handler"] = handler

    browser_context = MagicMock()
    browser_context.on = on_page
    browser_context.pages = []

    set_download_file_listener(browser_context)

    page = MagicMock()
    page_handlers: dict[str, object] = {}
    page.on = lambda _event, handler: page_handlers.setdefault("download", handler)
    captured_handler["handler"](page)

    ctx = SkyvernContext(workflow_run_id="wr_real", task_id="task_real")
    with (
        patch("skyvern.webeye.browser_factory.current", return_value=ctx),
        patch("skyvern.webeye.browser_factory.LOG.info", side_effect=capture_log),
    ):
        await page_handlers["download"](download)

    assert captured.get("workflow_run_id") == "wr_real"
    assert captured.get("task_id") == "task_real"


@pytest.mark.asyncio
async def test_listener_falls_back_to_kwargs_without_context(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def capture_log(_msg: str, **kwargs: object) -> None:
        captured.update(kwargs)

    extensionless_file = tmp_path / "statement"
    extensionless_file.write_bytes(b"data")
    download = MagicMock()
    download.suggested_filename = "statement"
    download.url = "https://example.com/d"
    download.path = AsyncMock(return_value=extensionless_file)

    captured_handler: dict[str, object] = {}
    browser_context = MagicMock()
    browser_context.on = lambda _event, handler: captured_handler.setdefault("handler", handler)
    browser_context.pages = []

    set_download_file_listener(browser_context, workflow_run_id="wr_kwarg", task_id="task_kwarg")

    page = MagicMock()
    page_handlers: dict[str, object] = {}
    page.on = lambda _event, handler: page_handlers.setdefault("download", handler)
    captured_handler["handler"](page)

    with (
        patch("skyvern.webeye.browser_factory.current", return_value=None),
        patch("skyvern.webeye.browser_factory.LOG.info", side_effect=capture_log),
    ):
        await page_handlers["download"](download)

    assert captured.get("workflow_run_id") == "wr_kwarg"
    assert captured.get("task_id") == "task_kwarg"


_SENTINEL_STEM = "zz-sentinel-suggested-stem"
_SENTINEL_QUERY_NAME = "zz-sentinel-query-stem"


async def _run_listener(download: MagicMock) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []

    def capture(_msg: str, **kwargs: object) -> None:
        events.append(kwargs)

    captured_handler: dict[str, object] = {}
    browser_context = MagicMock()
    browser_context.on = lambda _event, handler: captured_handler.setdefault("handler", handler)
    browser_context.pages = []
    set_download_file_listener(browser_context, workflow_run_id="wr_priv", task_id="task_priv")

    page = MagicMock()
    page_handlers: dict[str, object] = {}
    page.on = lambda _event, handler: page_handlers.setdefault("download", handler)
    captured_handler["handler"](page)

    with (
        patch("skyvern.webeye.browser_factory.current", return_value=None),
        patch("skyvern.webeye.browser_factory.LOG.info", side_effect=capture),
        patch("skyvern.webeye.browser_factory.LOG.debug", side_effect=capture),
    ):
        await page_handlers["download"](download)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("path_kind", ["absent_on_connection", "extensionless_suggestion", "url_query_filename"])
async def test_listener_never_logs_a_raw_download_filename(tmp_path: Path, path_kind: str) -> None:
    download = MagicMock()
    download.suggested_filename = _SENTINEL_STEM
    download.url = "https://example.com/d"
    if path_kind == "absent_on_connection":
        download.path = AsyncMock(return_value=tmp_path / "never-written")
    else:
        landed = tmp_path / "landed"
        landed.write_bytes(b"data")
        download.path = AsyncMock(return_value=landed)
    if path_kind == "url_query_filename":
        download.url = f"https://example.com/d?filename={_SENTINEL_QUERY_NAME}.pdf"

    events = await _run_listener(download)

    assert events, "the listener emitted no row for this path"
    logged = " ".join(f"{key}={value}" for event in events for key, value in event.items())
    assert _SENTINEL_STEM not in logged
    assert _SENTINEL_QUERY_NAME not in logged
    expected_field = "filename_fp" if path_kind == "url_query_filename" else "suggested_filename_fp"
    assert any(expected_field in event for event in events)


@pytest.mark.asyncio
async def test_browser_manager_adoption_rebinds_to_run_dir(browser_manager: RealBrowserManager) -> None:
    """The persistent-session attach path rebinds the adopted CDP downloadPath to the run dir."""
    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    browser_state.browser_artifacts.download_binding = DownloadBinding.RUN_DIR
    browser_state.get_working_page = AsyncMock(return_value=None)
    browser_state.get_or_create_page = AsyncMock()
    manager = browser_manager

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.skyvern_context.current", return_value=None),
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
        mock_app.AGENT_FUNCTION.on_browser_context_acquired = AsyncMock()
        result = await manager.get_or_create_for_workflow_run(
            _workflow_attach_run(),
            browser_session_id="bs_block",
        )

    assert result is browser_state
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"] == get_download_dir("wr_block")
    assert params["downloadPath"].endswith("/wr_block")


@pytest.mark.asyncio
async def test_browser_manager_adoption_skips_rebind_when_session_dir_binding(
    browser_manager: RealBrowserManager,
) -> None:
    """A provider-owned remote binding preserves the provider-selected destination."""
    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    browser_state.browser_artifacts.download_binding = DownloadBinding.SESSION_DIR
    browser_state.get_working_page = AsyncMock(return_value=None)
    browser_state.get_or_create_page = AsyncMock()
    manager = browser_manager

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.skyvern_context.current", return_value=None),
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
        mock_app.AGENT_FUNCTION.on_browser_context_acquired = AsyncMock()
        result = await manager.get_or_create_for_workflow_run(
            _workflow_attach_run(),
            browser_session_id="bs_block",
        )

    assert result is browser_state
    cdp_session.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_manager_adoption_rebinds_via_context_page_without_owning_browser(
    browser_manager: RealBrowserManager,
) -> None:
    """Persistent-context adoption rebinds through its working page when no Browser is exposed."""
    page, context, cdp_session = _recording_context_page()
    browser_state = MagicMock()
    browser_state.browser_context = context
    browser_state.browser_context.browser = None
    browser_state.browser_artifacts.download_binding = DownloadBinding.RUN_DIR
    browser_state.get_working_page = AsyncMock(side_effect=[page, page])
    browser_state.get_or_create_page = AsyncMock()
    manager = browser_manager

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.skyvern_context.current", return_value=None),
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
        mock_app.AGENT_FUNCTION.on_browser_context_acquired = AsyncMock()
        result = await manager.get_or_create_for_workflow_run(
            _workflow_attach_run(),
            browser_session_id="bs_block",
        )

    assert result is browser_state
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"].endswith("/wr_block")


@pytest.mark.asyncio
async def test_browser_manager_adoption_no_browser_no_page_returns_state(browser_manager: RealBrowserManager) -> None:
    """No Browser and no working page leaves nothing to rebind, but attachment stays fail-open."""
    browser_state = MagicMock()
    browser_state.browser_context.browser = None
    browser_state.browser_artifacts.download_binding = DownloadBinding.RUN_DIR
    browser_state.get_working_page = AsyncMock(return_value=None)
    browser_state.get_or_create_page = AsyncMock()
    manager = browser_manager

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.skyvern_context.current", return_value=None),
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
        mock_app.AGENT_FUNCTION.on_browser_context_acquired = AsyncMock()
        result = await manager.get_or_create_for_workflow_run(
            _workflow_attach_run(),
            browser_session_id="bs_block",
        )

    assert result is browser_state


@pytest.mark.asyncio
async def test_block_non_adoption_cache_hit_rebinds_to_run_dir() -> None:
    """Non-adoption acquisition (no browser_session_id) rebinds the cached CDP downloadPath."""

    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    browser_state.is_connected = MagicMock(return_value=True)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=browser_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_own",
            organization_id=None,
            browser_session_id=None,
            download_run_id_override="wr_own",
        )

    assert result is browser_state
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"] == get_download_dir("wr_own")
    assert params["downloadPath"].endswith("/wr_own")


@pytest.mark.asyncio
async def test_block_non_adoption_skips_rebind_when_session_dir_binding() -> None:
    """Defense-in-depth: an own-browser state carrying a SESSION_DIR binding must not be rebound off its
    provider-selected destination even on the non-adoption path."""

    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    browser_state.is_connected = MagicMock(return_value=True)
    browser_state.browser_artifacts.download_binding = DownloadBinding.SESSION_DIR

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=browser_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_own",
            organization_id=None,
            browser_session_id=None,
            download_run_id_override="wr_own",
        )

    assert result is browser_state
    cdp_session.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_block_non_adoption_rebinds_when_org_id_missing() -> None:
    """browser_session_id set but organization_id None is still non-adoption -> rebind fires."""

    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    browser_state.is_connected = MagicMock(return_value=True)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=browser_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_own",
            organization_id=None,
            browser_session_id="bs_x",
            download_run_id_override="wr_own",
        )

    assert result is browser_state
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"].endswith("/wr_own")


@pytest.mark.asyncio
async def test_block_non_adoption_rebinds_via_context_page_when_browser_is_none() -> None:
    """Persistent local context (browser_context.browser is None) rebinds via the working page's CDP session."""

    page, context, cdp_session = _recording_context_page()
    browser_state = MagicMock()
    browser_state.browser_context.browser = None
    browser_state.is_connected = MagicMock(return_value=True)
    browser_state.get_working_page = AsyncMock(return_value=page)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=browser_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_own",
            organization_id=None,
            browser_session_id=None,
            download_run_id_override="wr_own",
        )

    assert result is browser_state
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"].endswith("/wr_own")


@pytest.mark.asyncio
async def test_block_non_adoption_fresh_create_empty_context_rebinds_with_workflow_run_id() -> None:
    """No cached state + empty SkyvernContext -> fresh-create rebind keys off workflow_run_id."""

    browser, cdp_session = _recording_browser()
    created_state = MagicMock()
    created_state.browser_context.browser = browser
    created_state.is_connected = MagicMock(return_value=True)
    created_state.check_and_fix_state = AsyncMock()
    empty_ctx = SkyvernContext(run_id=None, workflow_run_id=None, task_id=None)

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=empty_ctx),
    ):
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=None)
        mock_app.WORKFLOW_SERVICE.get_workflow_run = AsyncMock(return_value=MagicMock())
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock(return_value=created_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_fresh",
            organization_id=None,
            browser_session_id=None,
        )

    assert result is created_state
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"].endswith("/wr_fresh")
    assert "None" not in params["downloadPath"]


@pytest.mark.asyncio
async def test_block_non_adoption_rebind_fail_open() -> None:
    """A non-adoption rebind failure is swallowed; the block still gets its browser state."""
    browser_state = MagicMock()
    browser_state.browser_context.browser = MagicMock()
    browser_state.is_connected = MagicMock(return_value=True)

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch(
            "skyvern.forge.sdk.workflow.models.block.rebind_download_dir",
            new_callable=AsyncMock,
            side_effect=RuntimeError("cdp down"),
        ),
    ):
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=browser_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_own",
            organization_id=None,
            browser_session_id=None,
            download_run_id_override="wr_own",
        )

    assert result is browser_state


@pytest.mark.asyncio
async def test_browser_manager_adoption_rebind_error_is_fail_open(browser_manager: RealBrowserManager) -> None:
    """A manager-owned rebind failure is swallowed and attachment still returns the browser state."""
    browser_state = MagicMock()
    browser_state.browser_context.browser = MagicMock()
    browser_state.browser_artifacts.download_binding = DownloadBinding.RUN_DIR
    browser_state.get_working_page = AsyncMock(return_value=None)
    browser_state.get_or_create_page = AsyncMock()
    manager = browser_manager

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.skyvern_context.current", return_value=None),
        patch(
            "skyvern.webeye.real_browser_manager.rebind_download_dir",
            new_callable=AsyncMock,
            side_effect=RuntimeError("cdp down"),
        ),
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
        mock_app.AGENT_FUNCTION.on_browser_context_acquired = AsyncMock()
        result = await manager.get_or_create_for_workflow_run(
            _workflow_attach_run(),
            browser_session_id="bs_block",
        )

    assert result is browser_state


def test_resolve_run_download_id_prefers_run_id() -> None:
    ctx = SkyvernContext(run_id="run_x", workflow_run_id="wr_y", task_id="t_z")
    assert resolve_run_download_id(ctx, fallback_run_id="fb") == "run_x"


def test_resolve_run_download_id_falls_back_through_workflow_then_task() -> None:
    assert resolve_run_download_id(SkyvernContext(run_id=None, workflow_run_id="wr_y", task_id="t_z")) == "wr_y"
    assert resolve_run_download_id(SkyvernContext(run_id=None, workflow_run_id=None, task_id="t_z")) == "t_z"


def test_resolve_run_download_id_uses_fallback_when_context_empty() -> None:
    assert resolve_run_download_id(None, fallback_run_id="fb") == "fb"
    empty = SkyvernContext(run_id=None, workflow_run_id=None, task_id=None)
    assert resolve_run_download_id(empty, fallback_run_id="fb") == "fb"


@pytest.mark.asyncio
async def test_block_adoption_delegates_runnable_identity_to_browser_manager() -> None:
    """The block passes immutable owner identity to the manager and never attaches directly."""
    browser_state = MagicMock()
    workflow_run = _workflow_attach_run()
    ctx = SkyvernContext(
        run_id="run_ctx",
        workflow_run_id="wr_block",
        browser_session_runnable_id="wr_root",
        browser_session_runnable_generation_id="gen_root",
    )

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=ctx),
    ):
        mock_app.WORKFLOW_SERVICE.get_workflow_run = AsyncMock(return_value=workflow_run)
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock(return_value=browser_state)
        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_block",
            organization_id="org_1",
            browser_session_id="bs_block",
        )

    assert result is browser_state
    mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run.assert_awaited_once_with(
        workflow_run=workflow_run,
        url=None,
        browser_session_id="bs_block",
        browser_profile_id=None,
        browser_session_runnable_id="wr_root",
        browser_session_runnable_generation_id="gen_root",
    )
    mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_not_called()


@pytest.mark.asyncio
async def test_block_adoption_task_context_delegates_to_task_attach_path() -> None:
    """A task-owned block uses the task attach path while retaining the context's owner identity."""
    browser_state = MagicMock()
    task = MagicMock(task_id="tsk_block")
    ctx = SkyvernContext(
        task_id="tsk_block",
        workflow_run_id="wr_block",
        browser_session_runnable_id="wr_root",
        browser_session_runnable_generation_id="gen_root",
    )

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=ctx),
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.BROWSER_MANAGER.get_or_create_for_task = AsyncMock(return_value=browser_state)
        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_block",
            organization_id="org_1",
            browser_session_id="bs_block",
        )

    assert result is browser_state
    mock_app.DATABASE.tasks.get_task.assert_awaited_once_with(
        task_id="tsk_block",
        organization_id="org_1",
    )
    mock_app.BROWSER_MANAGER.get_or_create_for_task.assert_awaited_once_with(
        task=task,
        browser_session_id="bs_block",
    )
    mock_app.WORKFLOW_SERVICE.get_workflow_run.assert_not_called()
    mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_not_called()


@pytest.mark.asyncio
async def test_file_upload_block_empty_scan_without_registered_downloads_succeeds(tmp_path) -> None:
    """SKY-11225: zero downloads during the run is a successful no-op."""
    from skyvern.forge.sdk.workflow.models.block import FileUploadBlock
    from skyvern.schemas.workflows import BlockStatus, FileStorageType

    block = FileUploadBlock.model_construct(
        label="upload",
        storage_type=FileStorageType.AZURE,
        azure_storage_account_name="account",
        azure_storage_account_key="key",
        azure_blob_container_name="container",
        path=None,
        continue_on_empty=False,
    )
    empty_dir = tmp_path / "wr_empty"
    empty_dir.mkdir()
    sentinel = object()
    workflow_run_context = MagicMock()
    workflow_run_context.organization_id = "org_1"

    with (
        patch.object(FileUploadBlock, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(FileUploadBlock, "format_potential_template_parameters", return_value=None),
        patch.object(FileUploadBlock, "record_output_parameter_value", new_callable=AsyncMock) as mock_record,
        patch.object(
            FileUploadBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel
        ) as mock_result,
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])
        mock_app.AGENT_FUNCTION.upload_file_to_customer_storage = AsyncMock()
        result = await block.execute(
            workflow_run_id="wr_empty",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
        )

    assert result is sentinel
    assert mock_result.await_args.kwargs["success"] is True
    assert mock_result.await_args.kwargs["status"] == BlockStatus.completed
    assert mock_result.await_args.kwargs["failure_reason"] is None
    assert mock_result.await_args.kwargs["output_parameter_value"] == []
    mock_record.assert_awaited_once()
    mock_app.AGENT_FUNCTION.upload_file_to_customer_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_upload_block_empty_scan_with_registered_downloads_fails(tmp_path) -> None:
    """SKY-11153/SKY-11225: downloaded files with an empty scan dir still fail loudly."""
    from skyvern.forge.sdk.schemas.files import FileInfo
    from skyvern.forge.sdk.workflow.models.block import FileUploadBlock
    from skyvern.schemas.workflows import BlockStatus, FileStorageType

    block = FileUploadBlock.model_construct(
        label="upload",
        storage_type=FileStorageType.S3,
        s3_bucket="bucket",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        path=None,
        continue_on_empty=False,
    )
    empty_dir = tmp_path / "wr_empty"
    empty_dir.mkdir()
    sentinel = object()
    workflow_run_context = MagicMock()
    workflow_run_context.organization_id = "org_1"

    with (
        patch.object(FileUploadBlock, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(FileUploadBlock, "format_potential_template_parameters", return_value=None),
        patch.object(FileUploadBlock, "record_output_parameter_value", new_callable=AsyncMock) as mock_record,
        patch.object(
            FileUploadBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel
        ) as mock_result,
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.STORAGE.get_downloaded_files = AsyncMock(
            return_value=[FileInfo(url="https://example.com/invoice.pdf", filename="invoice.pdf")]
        )
        result = await block.execute(
            workflow_run_id="wr_empty",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
        )

    assert result is sentinel
    assert mock_result.await_args.kwargs["success"] is False
    assert mock_result.await_args.kwargs["status"] == BlockStatus.failed
    assert "registered_download_count=1" in mock_result.await_args.kwargs["failure_reason"]
    mock_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_upload_block_empty_scan_with_alternate_download_dir_files_fails(tmp_path) -> None:
    """SKY-11225: local files in a sibling candidate dir still indicate a download-dir desync."""
    from skyvern.forge.sdk.workflow.models.block import FileUploadBlock
    from skyvern.schemas.workflows import BlockStatus, FileStorageType

    block = FileUploadBlock.model_construct(
        label="upload",
        storage_type=FileStorageType.S3,
        s3_bucket="bucket",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        path=None,
        continue_on_empty=False,
    )
    scan_dir = tmp_path / "run_ctx"
    alternate_dir = tmp_path / "wr_empty"
    scan_dir.mkdir()
    alternate_dir.mkdir()
    (alternate_dir / "invoice.pdf").write_text("pdf")
    sentinel = object()
    workflow_run_context = MagicMock()
    workflow_run_context.organization_id = "org_1"
    context = SkyvernContext(run_id="run_ctx", workflow_run_id="wr_empty")

    def get_download_dir_for_run_id(run_id: str | None):
        return {"run_ctx": scan_dir, "wr_empty": alternate_dir}[run_id]

    with (
        patch.object(FileUploadBlock, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(FileUploadBlock, "format_potential_template_parameters", return_value=None),
        patch.object(FileUploadBlock, "record_output_parameter_value", new_callable=AsyncMock) as mock_record,
        patch.object(
            FileUploadBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel
        ) as mock_result,
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            side_effect=get_download_dir_for_run_id,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=context),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])
        mock_app.AGENT_FUNCTION.upload_file_to_customer_storage = AsyncMock()
        result = await block.execute(
            workflow_run_id="wr_empty",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
        )

    assert result is sentinel
    assert mock_result.await_args.kwargs["success"] is False
    assert mock_result.await_args.kwargs["status"] == BlockStatus.failed
    assert "alternate_file_count=1" in mock_result.await_args.kwargs["failure_reason"]
    mock_record.assert_not_awaited()
    mock_app.AGENT_FUNCTION.upload_file_to_customer_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_upload_block_empty_scan_with_too_many_alternate_files_reports_too_many(tmp_path) -> None:
    """SKY-11225: oversized alternate dirs fail closed with a specific diagnostic."""
    from skyvern.constants import MAX_UPLOAD_FILE_COUNT
    from skyvern.forge.sdk.workflow.models.block import FileUploadBlock
    from skyvern.schemas.workflows import BlockStatus, FileStorageType

    block = FileUploadBlock.model_construct(
        label="upload",
        storage_type=FileStorageType.S3,
        s3_bucket="bucket",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        path=None,
        continue_on_empty=False,
    )
    scan_dir = tmp_path / "run_ctx"
    alternate_dir = tmp_path / "wr_empty"
    scan_dir.mkdir()
    alternate_dir.mkdir()
    for index in range(MAX_UPLOAD_FILE_COUNT + 1):
        (alternate_dir / f"invoice_{index}.pdf").write_text("pdf")
    sentinel = object()
    workflow_run_context = MagicMock()
    workflow_run_context.organization_id = "org_1"
    context = SkyvernContext(run_id="run_ctx", workflow_run_id="wr_empty")

    def get_download_dir_for_run_id(run_id: str | None):
        return {"run_ctx": scan_dir, "wr_empty": alternate_dir}[run_id]

    with (
        patch.object(FileUploadBlock, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(FileUploadBlock, "format_potential_template_parameters", return_value=None),
        patch.object(FileUploadBlock, "record_output_parameter_value", new_callable=AsyncMock) as mock_record,
        patch.object(
            FileUploadBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel
        ) as mock_result,
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            side_effect=get_download_dir_for_run_id,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=context),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])
        mock_app.AGENT_FUNCTION.upload_file_to_customer_storage = AsyncMock()
        result = await block.execute(
            workflow_run_id="wr_empty",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
        )

    assert result is sentinel
    assert mock_result.await_args.kwargs["success"] is False
    assert mock_result.await_args.kwargs["status"] == BlockStatus.failed
    assert "alternate_file_count=too_many" in mock_result.await_args.kwargs["failure_reason"]
    mock_record.assert_not_awaited()
    mock_app.AGENT_FUNCTION.upload_file_to_customer_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_upload_block_empty_scan_with_browser_session_downloads_fails(tmp_path) -> None:
    """SKY-11225: unclaimed browser-session downloads are not a benign empty run."""
    from skyvern.forge.sdk.workflow.models.block import FileUploadBlock
    from skyvern.schemas.workflows import BlockStatus, FileStorageType

    block = FileUploadBlock.model_construct(
        label="upload",
        storage_type=FileStorageType.S3,
        s3_bucket="bucket",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        path=None,
        continue_on_empty=False,
    )
    empty_dir = tmp_path / "wr_empty"
    empty_dir.mkdir()
    sentinel = object()
    workflow_run_context = MagicMock()
    workflow_run_context.organization_id = "org_1"

    with (
        patch.object(FileUploadBlock, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(FileUploadBlock, "format_potential_template_parameters", return_value=None),
        patch.object(FileUploadBlock, "record_output_parameter_value", new_callable=AsyncMock) as mock_record,
        patch.object(
            FileUploadBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel
        ) as mock_result,
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(
            return_value=["s3://downloads/session/invoice.pdf"]
        )
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])
        mock_app.AGENT_FUNCTION.upload_file_to_customer_storage = AsyncMock()
        result = await block.execute(
            workflow_run_id="wr_empty",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
            browser_session_id="pbs_1",
        )

    assert result is sentinel
    assert mock_result.await_args.kwargs["success"] is False
    assert mock_result.await_args.kwargs["status"] == BlockStatus.failed
    assert "browser_session_download_count=1" in mock_result.await_args.kwargs["failure_reason"]
    mock_record.assert_not_awaited()
    mock_app.AGENT_FUNCTION.upload_file_to_customer_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_upload_block_empty_scan_registered_download_timeout_fails_with_unknown_count(tmp_path) -> None:
    """SKY-11225: unknown registered-download state fails closed with a readable failure reason."""
    import asyncio

    from skyvern.forge.sdk.workflow.models.block import FileUploadBlock
    from skyvern.schemas.workflows import BlockStatus, FileStorageType

    block = FileUploadBlock.model_construct(
        label="upload",
        storage_type=FileStorageType.S3,
        s3_bucket="bucket",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        path=None,
        continue_on_empty=False,
    )
    empty_dir = tmp_path / "wr_empty"
    empty_dir.mkdir()
    sentinel = object()
    workflow_run_context = MagicMock()
    workflow_run_context.organization_id = "org_1"

    with (
        patch.object(FileUploadBlock, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(FileUploadBlock, "format_potential_template_parameters", return_value=None),
        patch.object(FileUploadBlock, "record_output_parameter_value", new_callable=AsyncMock) as mock_record,
        patch.object(
            FileUploadBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel
        ) as mock_result,
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.STORAGE.get_downloaded_files = AsyncMock(side_effect=asyncio.TimeoutError)
        result = await block.execute(
            workflow_run_id="wr_empty",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
        )

    assert result is sentinel
    assert mock_result.await_args.kwargs["success"] is False
    assert mock_result.await_args.kwargs["status"] == BlockStatus.failed
    assert "registered_download_count=unknown" in mock_result.await_args.kwargs["failure_reason"]
    mock_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_upload_block_continue_on_empty_succeeds(tmp_path) -> None:
    """SKY-11153 / RISK-1: continue_on_empty=True preserves prior semantics — empty dir -> success."""
    from skyvern.forge.sdk.workflow.models.block import FileUploadBlock
    from skyvern.schemas.workflows import FileStorageType

    block = FileUploadBlock.model_construct(
        label="upload",
        storage_type=FileStorageType.S3,
        s3_bucket="bucket",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        path=None,
        continue_on_empty=True,
    )
    empty_dir = tmp_path / "wr_empty2"
    empty_dir.mkdir()
    sentinel = object()

    with (
        patch.object(FileUploadBlock, "get_workflow_run_context", return_value=MagicMock()),
        patch.object(FileUploadBlock, "format_potential_template_parameters", return_value=None),
        patch.object(FileUploadBlock, "record_output_parameter_value", new_callable=AsyncMock),
        patch.object(
            FileUploadBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel
        ) as mock_result,
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.AGENT_FUNCTION.upload_file_to_customer_storage = AsyncMock()
        result = await block.execute(
            workflow_run_id="wr_empty2",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
        )

    assert result is sentinel
    assert mock_result.await_args.kwargs["success"] is True
    # The success is a true no-op, not an accidental upload (claude-review hardening).
    mock_app.AGENT_FUNCTION.upload_file_to_customer_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_upload_block_uploads_downloads_to_google_drive(tmp_path) -> None:
    from types import SimpleNamespace

    from skyvern.forge.sdk.workflow.models.block import FileUploadBlock
    from skyvern.schemas.workflows import FileStorageType

    download_dir = tmp_path / "wr_drive"
    download_dir.mkdir()
    source = download_dir / "report.txt"
    source.write_text("drive upload")

    block = FileUploadBlock.model_construct(
        label="upload",
        storage_type=FileStorageType.GOOGLE_DRIVE,
        google_credential_id="goac_123",
        google_drive_folder_id="https://drive.google.com/drive/folders/folder_123",
        path=None,
        continue_on_empty=False,
    )
    sentinel = object()
    workflow_run_context = MagicMock()
    workflow_run_context.organization_id = "org_1"
    workflow_run_context.get_original_secret_value_or_none.return_value = None

    with (
        patch.object(FileUploadBlock, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(FileUploadBlock, "format_potential_template_parameters", return_value=None),
        patch.object(FileUploadBlock, "record_output_parameter_value", new_callable=AsyncMock),
        patch.object(
            FileUploadBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel
        ) as mock_result,
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            return_value=download_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.AGENT_FUNCTION.get_google_workspace_credentials = AsyncMock(return_value=SimpleNamespace(token="at-1"))
        mock_app.AGENT_FUNCTION.upload_file_to_customer_storage = AsyncMock(
            return_value="https://drive.google.com/file/d/file_123/view"
        )
        result = await block.execute(
            workflow_run_id="wr_drive",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
        )

    assert result is sentinel
    assert mock_result.await_args.kwargs["success"] is True
    assert mock_result.await_args.kwargs["output_parameter_value"] == ["https://drive.google.com/file/d/file_123/view"]
    mock_app.AGENT_FUNCTION.upload_file_to_customer_storage.assert_awaited_once()
    upload_kwargs = mock_app.AGENT_FUNCTION.upload_file_to_customer_storage.await_args.kwargs
    assert upload_kwargs["file_path"] == str(source)
    assert upload_kwargs["organization_id"] == "org_1"
    assert upload_kwargs["run_id"] == "wr_drive"
    destination = upload_kwargs["destination"]
    assert destination.storage_type == FileStorageType.GOOGLE_DRIVE
    assert destination.google_access_token == "at-1"
    assert destination.google_drive_folder_id == "folder_123"


def test_resolve_run_download_id_preserves_task_id_tail() -> None:
    """CORR-1: mirrors handler.py fallback_run_id=task.workflow_run_id or task.task_id — when both
    context and workflow_run_id are absent, the task_id tail must still be resolved (not None)."""
    empty = SkyvernContext(run_id=None, workflow_run_id=None, task_id=None)
    # Equivalent to handler.py's `task.workflow_run_id or task.task_id` collapsing to the task_id tail.
    assert resolve_run_download_id(empty, fallback_run_id="tsk_x") == "tsk_x"


@pytest.mark.asyncio
async def test_real_browser_manager_adoption_resolves_context_run_id(browser_manager: RealBrowserManager) -> None:
    """SKY-11153 / COMP-2: the RealBrowserManager adoption seam rebinds the adopted session's
    download dir to context.run_id-first, matching the block seam and FileUploadBlock."""

    manager = browser_manager
    workflow_run = MagicMock(
        workflow_run_id="wr_x", parent_workflow_run_id=None, browser_profile_id=None, organization_id="org_1"
    )
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)
    browser_state.get_or_create_page = AsyncMock()
    ctx = SkyvernContext(run_id="run_ctx", workflow_run_id="wr_x")

    with (
        patch.object(RealBrowserManager, "get_for_workflow_run", return_value=None),
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock) as mock_rebind,
        patch("skyvern.webeye.real_browser_manager.skyvern_context.current", return_value=ctx),
    ):
        mock_app.AGENT_FUNCTION.on_browser_context_acquired = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
        mock_app.DATABASE.browser_sessions.touch_last_activity = AsyncMock()
        result = await manager.get_or_create_for_workflow_run(workflow_run, browser_session_id="bs")

    assert result is browser_state
    mock_rebind.assert_awaited_once_with(browser_state.browser_context.browser, run_id="run_ctx")


@pytest.mark.asyncio
async def test_block_non_adoption_override_takes_precedence_over_context() -> None:
    """The CodeBlock-computed override is the storage key; the rebind binds the same id."""

    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    browser_state.is_connected = MagicMock(return_value=True)
    ctx = SkyvernContext(run_id=None, workflow_run_id="wr_block", task_id=None)

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=ctx),
    ):
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=browser_state)

        await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_block",
            organization_id=None,
            browser_session_id=None,
            download_run_id_override="run_ctx",
        )

    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"].endswith("/run_ctx")


@pytest.mark.asyncio
async def test_block_non_adoption_reused_browser_rebinds_to_second_run() -> None:
    """A second acquisition reusing a pooled workflow-run browser rebinds to the second run dir."""

    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    browser_state.is_connected = MagicMock(return_value=True)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=browser_state)

        await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_first",
            organization_id=None,
            browser_session_id=None,
            download_run_id_override="wr_first",
        )
        await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_second",
            organization_id=None,
            browser_session_id=None,
            download_run_id_override="wr_second",
        )

    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"].endswith("/wr_second")


@pytest.mark.asyncio
async def test_register_downloaded_files_reports_visibility_for_a_non_secure_engine(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is emitted from the shared registration seam, so engines are comparable.

    Emitting only from the secure-runner dispatch would make the population engine-biased,
    the same defect the unregistered-intent signal avoids by firing from every engine.
    """
    sentinel = "quarterly-policy-summary-sentinel.pdf"
    monkeypatch.setattr("skyvern.forge.sdk.api.files.settings.DOWNLOAD_PATH", str(tmp_path))
    monkeypatch.setattr("skyvern.forge.sdk.core.hashing.settings.SECRET_KEY", "download-observation-fingerprint-key")
    run_dir = tmp_path / "wr_block"
    run_dir.mkdir()
    (run_dir / sentinel).write_bytes(b"%PDF-1.4 sentinel")

    block = CodeBlock.__new__(CodeBlock)
    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])

        with capture_logs() as logs:
            await block._register_downloaded_files(
                engine="inline",
                organization_id="org_1",
                workflow_run_id="wr_block",
                workflow_run_block_id="wrb_x",
            )

    rows = [entry for entry in logs if entry.get("event") == "codeblock.download_registration_visibility"]
    assert len(rows) == 1
    row = rows[0]
    # The seam row is a separate emission site from the secure wrapper, so AC2 is asserted here too.
    assert row["post_entry_count"] == 1
    assert row["post_entry_fps"]
    rendered = repr(row)
    assert sentinel not in rendered
    assert "policy-summary" not in rendered
    assert ".pdf" not in rendered
    assert row["engine"] == "inline"
    assert row["boundary"] == "register"
    assert row["landed_during_settle"] is None
    # The engine-neutral row still has to answer AC1's timing question on its own.
    assert row["landed_between_snapshots"] is False


@pytest.mark.asyncio
async def test_register_downloaded_files_uses_download_run_id_as_storage_key() -> None:
    """_register_downloaded_files keys storage on download_run_id, not the raw workflow_run_id."""

    block = CodeBlock.__new__(CodeBlock)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])

        await block._register_downloaded_files(
            engine="inline",
            organization_id="org_1",
            workflow_run_id="wr_block",
            workflow_run_block_id="wrb_x",
            download_run_id="run_ctx",
        )

    assert mock_app.STORAGE.save_downloaded_files.await_args.kwargs["run_id"] == "run_ctx"
    assert mock_app.STORAGE.get_downloaded_files.await_args.kwargs["run_id"] == "run_ctx"


@pytest.mark.asyncio
async def test_register_downloaded_files_defaults_to_workflow_run_id() -> None:
    """download_run_id=None falls back to workflow_run_id for back-compat."""

    block = CodeBlock.__new__(CodeBlock)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])

        await block._register_downloaded_files(
            engine="inline",
            organization_id="org_1",
            workflow_run_id="wr_block",
            workflow_run_block_id="wrb_x",
        )

    assert mock_app.STORAGE.save_downloaded_files.await_args.kwargs["run_id"] == "wr_block"


@pytest.mark.asyncio
async def test_register_pdf_uses_download_run_id_as_storage_key() -> None:
    """PrintPageBlock registration keys storage on download_run_id, not the raw workflow_run_id."""

    block = PrintPageBlock.__new__(PrintPageBlock)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])

        await block._register_pdf_as_downloaded_file(
            organization_id="org_1",
            workflow_run_id="wr_block",
            workflow_run_block_id="wrb_x",
            download_run_id="run_ctx",
        )

    assert mock_app.STORAGE.save_downloaded_files.await_args.kwargs["run_id"] == "run_ctx"
    assert mock_app.STORAGE.get_downloaded_files.await_args.kwargs["run_id"] == "run_ctx"


@pytest.mark.asyncio
async def test_register_pdf_defaults_to_workflow_run_id() -> None:
    """download_run_id=None falls back to workflow_run_id for back-compat."""

    block = PrintPageBlock.__new__(PrintPageBlock)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])

        await block._register_pdf_as_downloaded_file(
            organization_id="org_1",
            workflow_run_id="wr_block",
            workflow_run_block_id="wrb_x",
        )

    assert mock_app.STORAGE.save_downloaded_files.await_args.kwargs["run_id"] == "wr_block"


@pytest.mark.asyncio
async def test_print_page_block_threads_resolved_id_to_all_sinks(tmp_path) -> None:
    """When context.run_id != workflow_run_id, PrintPageBlock binds the rebind override, the PDF
    file-write dir, and the registration storage key all to the resolved download id."""

    block = PrintPageBlock.model_construct(
        label="print",
        include_timestamp=False,
        custom_filename=None,
        format="A4",
        landscape=False,
        print_background=True,
        parameters=[],
    )
    sentinel = object()
    workflow_run_context = MagicMock()
    workflow_run_context.organization_id = "org_1"
    ctx = SkyvernContext(run_id="run_ctx", workflow_run_id="wr_block", task_id=None)

    page = MagicMock()
    page.pdf = AsyncMock(return_value=b"%PDF-1.4 fake")
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=page)

    captured: dict[str, object] = {}

    async def fake_get_or_create(self, **kwargs):
        captured["override"] = kwargs.get("download_run_id_override")
        return browser_state

    async def fake_register(self, **kwargs):
        captured["register_download_run_id"] = kwargs.get("download_run_id")
        return []

    def fake_get_download_dir(run_id):
        captured["file_write_run_id"] = run_id
        return str(tmp_path)

    async def fake_get_downloaded_files(*, organization_id, run_id):
        captured["baseline_run_id"] = run_id
        return []

    with (
        patch.object(PrintPageBlock, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(PrintPageBlock, "get_or_create_browser_state", new=fake_get_or_create),
        patch.object(PrintPageBlock, "_register_pdf_as_downloaded_file", new=fake_register),
        patch.object(PrintPageBlock, "record_output_parameter_value", new_callable=AsyncMock),
        patch.object(PrintPageBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel),
        patch.object(
            PrintPageBlock,
            "_upload_pdf_artifact",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch("skyvern.forge.sdk.workflow.models.block.get_download_dir", side_effect=fake_get_download_dir),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
    ):
        mock_app.STORAGE.get_downloaded_files = AsyncMock(side_effect=fake_get_downloaded_files)
        result = await block.execute(
            workflow_run_id="wr_block",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
        )

    assert result is sentinel
    assert captured["override"] == "run_ctx"
    assert captured["file_write_run_id"] == "run_ctx"
    assert captured["register_download_run_id"] == "run_ctx"
    assert captured["baseline_run_id"] == "run_ctx"
