"""Run-scoped download dir binding for adopted persistent sessions (SKY-11083).

A persistent browser session launched outside any run context binds its CDP
downloadPath to downloads/None/. When a workflow run later adopts that session,
the dir must be rebound to downloads/<workflow_run_id>/ so downloads land
run-scoped and the listener logs the real run identity.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.api.files import get_download_dir, resolve_run_download_id
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.block import Block, CodeBlock, PrintPageBlock
from skyvern.webeye.browser_factory import (
    _apply_download_behaviour,
    rebind_download_dir,
    set_download_file_listener,
)


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

    interceptor.set_download_dir.assert_called_once_with(get_download_dir("wr_test"))


@pytest.mark.asyncio
async def test_rebind_ignores_context_without_interceptor() -> None:
    browser, cdp_session = _recording_browser()
    browser.contexts = [SimpleNamespace()]

    await rebind_download_dir(browser, run_id="wr_test")

    cdp_session.send.assert_awaited_once()


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

    interceptor.set_download_dir.assert_called_once_with(get_download_dir("wr_test"))


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

    interceptor.set_download_dir.assert_called_once_with(get_download_dir("wr_test"))


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

    interceptor.set_download_dir.assert_called_once_with(get_download_dir("wr_test"))
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

    interceptor.set_download_dir.assert_called_once_with(get_download_dir("wr_test"))
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
async def test_listener_logs_run_identity_from_context() -> None:
    captured: dict[str, object] = {}

    def capture_log(_msg: str, **kwargs: object) -> None:
        captured.update(kwargs)

    download = MagicMock()
    download.suggested_filename = "statement.pdf"
    download.url = "https://example.com/d"
    path = MagicMock()
    path.suffix = ""
    download.path = AsyncMock(return_value=path)

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
async def test_listener_falls_back_to_kwargs_without_context() -> None:
    captured: dict[str, object] = {}

    def capture_log(_msg: str, **kwargs: object) -> None:
        captured.update(kwargs)

    download = MagicMock()
    download.suggested_filename = "statement.pdf"
    download.url = "https://example.com/d"
    path = MagicMock()
    path.suffix = ""
    download.path = AsyncMock(return_value=path)

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


@pytest.mark.asyncio
async def test_block_adoption_seam_rebinds_to_run_dir() -> None:
    """The copilot code-block acquisition site rebinds the adopted CDP downloadPath to the run dir (SKY-11083)."""
    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock()

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_block",
            organization_id="org_1",
            browser_session_id="bs_block",
        )

    assert result is browser_state
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"] == get_download_dir("wr_block")
    assert params["downloadPath"].endswith("/wr_block")
    mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run.assert_not_called()


@pytest.mark.asyncio
async def test_block_adoption_seam_rebinds_via_context_page_without_owning_browser() -> None:
    """Persistent-context adoption (browser_context.browser is None) rebinds via the working page's CDP session."""
    page, context, cdp_session = _recording_context_page()
    browser_state = MagicMock()
    browser_state.browser_context.browser = None
    browser_state.is_connected = MagicMock(return_value=True)
    browser_state.get_working_page = AsyncMock(return_value=page)

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_block",
            organization_id="org_1",
            browser_session_id="bs_block",
        )

    assert result is browser_state
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"].endswith("/wr_block")


@pytest.mark.asyncio
async def test_block_adoption_seam_no_browser_no_page_returns_state() -> None:
    """No owning Browser and no working page -> nothing to rebind, still return the state (fail-open)."""
    browser_state = MagicMock()
    browser_state.browser_context.browser = None
    browser_state.is_connected = MagicMock(return_value=True)
    browser_state.get_working_page = AsyncMock(return_value=None)

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_block",
            organization_id="org_1",
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

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
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
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"] == get_download_dir("wr_own")
    assert params["downloadPath"].endswith("/wr_own")


@pytest.mark.asyncio
async def test_block_non_adoption_rebinds_when_org_id_missing() -> None:
    """browser_session_id set but organization_id None is still non-adoption -> rebind fires."""

    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    browser_state.is_connected = MagicMock(return_value=True)

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
    ):
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

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
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
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.skyvern_context.current", return_value=empty_ctx),
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
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
        patch(
            "skyvern.forge.sdk.workflow.models.block_base.rebind_download_dir",
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
async def test_block_adoption_seam_fail_open_on_rebind_error() -> None:
    """A rebind failure is swallowed; the block still receives its browser state (fail-open)."""
    browser_state = MagicMock()
    browser_state.browser_context.browser = MagicMock()

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
        patch(
            "skyvern.forge.sdk.workflow.models.block_base.rebind_download_dir",
            new_callable=AsyncMock,
            side_effect=RuntimeError("cdp down"),
        ),
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock()

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_block",
            organization_id="org_1",
            browser_session_id="bs_block",
        )

    assert result is browser_state
    mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run.assert_not_called()


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
async def test_block_adoption_prefers_context_run_id_over_workflow_run_id() -> None:
    """SKY-11153 regression: when context.run_id differs from workflow_run_id (e.g. task_v2),
    the adopted session's download dir must bind to context.run_id — the key FileUploadBlock
    scans — not the raw workflow_run_id."""
    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    ctx = SkyvernContext(run_id="run_ctx", workflow_run_id="wr_block")

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.skyvern_context.current", return_value=ctx),
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock()

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_block",
            organization_id="org_1",
            browser_session_id="bs_block",
        )

    assert result is browser_state
    _, params = cdp_session.send.await_args.args
    assert params["downloadPath"] == get_download_dir("run_ctx")
    assert params["downloadPath"].endswith("/run_ctx")


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
            "skyvern.forge.sdk.workflow.models.storage_blocks.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
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
            "skyvern.forge.sdk.workflow.models.storage_blocks.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
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
            "skyvern.forge.sdk.workflow.models.storage_blocks.get_path_for_workflow_download_directory",
            side_effect=get_download_dir_for_run_id,
        ),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.skyvern_context.current", return_value=context),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
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
            "skyvern.forge.sdk.workflow.models.storage_blocks.get_path_for_workflow_download_directory",
            side_effect=get_download_dir_for_run_id,
        ),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.skyvern_context.current", return_value=context),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
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
            "skyvern.forge.sdk.workflow.models.storage_blocks.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
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
            "skyvern.forge.sdk.workflow.models.storage_blocks.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
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
            "skyvern.forge.sdk.workflow.models.storage_blocks.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
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
            "skyvern.forge.sdk.workflow.models.storage_blocks.get_path_for_workflow_download_directory",
            return_value=download_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.skyvern_context.current", return_value=None),
        patch("skyvern.forge.sdk.workflow.models.storage_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
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
async def test_real_browser_manager_adoption_resolves_context_run_id() -> None:
    """SKY-11153 / COMP-2: the RealBrowserManager adoption seam rebinds the adopted session's
    download dir to context.run_id-first, matching the block seam and FileUploadBlock."""
    from skyvern.webeye.real_browser_manager import RealBrowserManager

    manager = RealBrowserManager.__new__(RealBrowserManager)
    manager.pages = {}
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
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)
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
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.skyvern_context.current", return_value=ctx),
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

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
    ):
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
async def test_register_downloaded_files_uses_download_run_id_as_storage_key() -> None:
    """_register_downloaded_files keys storage on download_run_id, not the raw workflow_run_id."""

    block = CodeBlock.__new__(CodeBlock)

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.code_block.app", mock_app),
    ):
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])

        await block._register_downloaded_files(
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

    with (
        patch("skyvern.forge.sdk.workflow.models.block_base.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.code_block.app", mock_app),
    ):
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])

        await block._register_downloaded_files(
            organization_id="org_1",
            workflow_run_id="wr_block",
            workflow_run_block_id="wrb_x",
        )

    assert mock_app.STORAGE.save_downloaded_files.await_args.kwargs["run_id"] == "wr_block"


@pytest.mark.asyncio
async def test_register_pdf_uses_download_run_id_as_storage_key() -> None:
    """PrintPageBlock registration keys storage on download_run_id, not the raw workflow_run_id."""

    block = PrintPageBlock.__new__(PrintPageBlock)

    with (
        patch("skyvern.forge.sdk.workflow.models.misc_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
        patch("skyvern.forge.sdk.workflow.models.misc_blocks.app", mock_app),
    ):
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

    with (
        patch("skyvern.forge.sdk.workflow.models.misc_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
        patch("skyvern.forge.sdk.workflow.models.misc_blocks.app", mock_app),
    ):
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
        patch("skyvern.forge.sdk.workflow.models.misc_blocks.get_download_dir", side_effect=fake_get_download_dir),
        patch("skyvern.forge.sdk.workflow.models.misc_blocks.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.sdk.workflow.models.misc_blocks.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block_base.app", mock_app),
        patch("skyvern.forge.sdk.workflow.models.misc_blocks.app", mock_app),
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
