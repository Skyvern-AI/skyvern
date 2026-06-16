"""Run-scoped download dir binding for adopted persistent sessions (SKY-11083).

A persistent browser session launched outside any run context binds its CDP
downloadPath to downloads/None/. When a workflow run later adopts that session,
the dir must be rebound to downloads/<workflow_run_id>/ so downloads land
run-scoped and the listener logs the real run identity.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.api.files import get_download_dir
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
