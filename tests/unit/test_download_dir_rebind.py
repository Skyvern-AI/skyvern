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
    from skyvern.forge.sdk.workflow.models.block import Block

    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
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
async def test_block_adoption_seam_skips_rebind_without_browser() -> None:
    """No owning Browser on the adopted context -> skip the rebind, still return the state."""
    from skyvern.forge.sdk.workflow.models.block import Block

    browser_state = MagicMock()
    browser_state.browser_context.browser = None

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block.rebind_download_dir", new_callable=AsyncMock) as mock_rebind,
    ):
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=browser_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_block",
            organization_id="org_1",
            browser_session_id="bs_block",
        )

    assert result is browser_state
    mock_rebind.assert_not_awaited()


@pytest.mark.asyncio
async def test_block_own_browser_path_does_not_rebind() -> None:
    """Own-browser acquisition (no browser_session_id) runs zero rebind code (SKY-11083 regression guard)."""
    from skyvern.forge.sdk.workflow.models.block import Block

    browser_state = MagicMock()

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block.rebind_download_dir", new_callable=AsyncMock) as mock_rebind,
    ):
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=browser_state)

        result = await Block.get_or_create_browser_state(
            MagicMock(),
            workflow_run_id="wr_own",
            organization_id=None,
            browser_session_id=None,
        )

    assert result is browser_state
    mock_rebind.assert_not_awaited()


@pytest.mark.asyncio
async def test_block_adoption_seam_fail_open_on_rebind_error() -> None:
    """A rebind failure is swallowed; the block still receives its browser state (fail-open)."""
    from skyvern.forge.sdk.workflow.models.block import Block

    browser_state = MagicMock()
    browser_state.browser_context.browser = MagicMock()

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch(
            "skyvern.forge.sdk.workflow.models.block.rebind_download_dir",
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
    from skyvern.forge.sdk.workflow.models.block import Block

    browser, cdp_session = _recording_browser()
    browser_state = MagicMock()
    browser_state.browser_context.browser = browser
    ctx = SkyvernContext(run_id="run_ctx", workflow_run_id="wr_block")

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=ctx),
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
async def test_file_upload_block_fails_when_no_files_found(tmp_path) -> None:
    """SKY-11153 regression: an empty download dir must fail the block, not silently report
    success with zero files uploaded."""
    from skyvern.forge.sdk.workflow.models.block import FileUploadBlock
    from skyvern.schemas.workflows import FileStorageType

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

    with (
        patch.object(FileUploadBlock, "get_workflow_run_context", return_value=MagicMock()),
        patch.object(FileUploadBlock, "format_potential_template_parameters", return_value=None),
        patch.object(
            FileUploadBlock, "build_block_result", new_callable=AsyncMock, return_value=sentinel
        ) as mock_result,
        patch(
            "skyvern.forge.sdk.workflow.models.block.get_path_for_workflow_download_directory",
            return_value=empty_dir,
        ),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
    ):
        result = await block.execute(
            workflow_run_id="wr_empty",
            workflow_run_block_id="wrb_x",
            organization_id="org_1",
        )

    assert result is sentinel
    assert mock_result.await_args.kwargs["success"] is False


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
