"""Tests for _wait_for_upload_processing helper and regression guards."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest
from playwright._impl._errors import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.webeye.actions import handler as handler_module
from skyvern.webeye.actions.actions import ClickAction, UploadFileAction
from skyvern.webeye.actions.handler import _wait_for_upload_processing
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection


class _EngineError(Exception):
    pass


class _EngineTimeout(_EngineError):
    pass


async def _never_start():  # pragma: no cover - never awaited
    raise AssertionError("start_driver must not be called")


def _engine_selection() -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name="engine-a",
        start_driver=_never_start,
        error_type=_EngineError,
        timeout_error_type=_EngineTimeout,
        metadata=BrowserEngineMetadata(name="engine-a", version="0.0.0"),
        selection_reason="test",
    )


async def _run_with_page_ready_error(error: BaseException, engine_selection: BrowserEngineSelection | None) -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = error
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        await _wait_for_upload_processing(AsyncMock(), engine_selection=engine_selection)


# ---------------------------------------------------------------------------
# Helper behaviour tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calls_wait_for_page_ready_with_settle_delay() -> None:
    page = AsyncMock()
    engine_selection = _engine_selection()
    mock_frame = AsyncMock()
    with (
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create,
        patch("skyvern.webeye.actions.handler._upload_settle_sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        mock_create.return_value = mock_frame
        sequence = MagicMock()
        sequence.attach_mock(mock_sleep, "sleep")
        sequence.attach_mock(mock_create, "create_frame")
        sequence.attach_mock(mock_frame.wait_for_page_ready, "wait_for_page_ready")
        await _wait_for_upload_processing(page, engine_selection=engine_selection)

    # Settle delay before readiness polling
    mock_sleep.assert_awaited_once_with(0.5)
    mock_create.assert_awaited_once_with(page, engine_selection=engine_selection)
    mock_frame.wait_for_page_ready.assert_awaited_once_with(
        loading_indicator_timeout_ms=3000,
        network_idle_timeout_ms=3000,
        dom_stable_ms=300,
        dom_stability_timeout_ms=2000,
    )
    assert sequence.mock_calls == [
        call.sleep(0.5),
        call.create_frame(page, engine_selection=engine_selection),
        call.wait_for_page_ready(
            loading_indicator_timeout_ms=3000,
            network_idle_timeout_ms=3000,
            dom_stable_ms=300,
            dom_stability_timeout_ms=2000,
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_selection", [_engine_selection(), None])
async def test_input_or_select_context_reuses_task_engine_selection(
    engine_selection: BrowserEngineSelection | None,
) -> None:
    task = Mock()
    step = Mock()
    frame = Mock()
    element = Mock()
    element.get_frame.return_value = frame
    element.get_element_handler = AsyncMock(return_value=Mock())
    element.get_frame_id.return_value = "frame-id"
    element.get_locator.return_value.locator.return_value.element_handle = AsyncMock(return_value=Mock())
    skyvern_frame = AsyncMock()
    skyvern_frame.get_element_dom_depth.return_value = 6
    skyvern_frame.build_tree_from_element.return_value = ([], [])
    cleanup_factory = Mock(return_value=AsyncMock(return_value=[]))
    app_instance = object.__getattribute__(handler_module.app, "_inst")

    with (
        patch.object(handler_module, "resolve_engine_selection_for_task", return_value=engine_selection) as resolve,
        patch.object(handler_module.SkyvernFrame, "create_instance", AsyncMock(return_value=skyvern_frame)) as create,
        patch.object(handler_module.app.AGENT_FUNCTION, "cleanup_element_tree_factory", cleanup_factory),
        patch.object(app_instance, "PARSE_SELECT_LLM_API_HANDLER", AsyncMock(return_value={}), create=True),
    ):
        await handler_module._get_input_or_select_context(
            action=handler_module.AbstractActionForContextParse(reasoning=None, element_id="element", intention=None),
            skyvern_element=element,
            element_tree_builder=Mock(),
            task=task,
            step=step,
            engine_selection=engine_selection,
        )
    resolve.assert_not_called()
    create.assert_awaited_once_with(frame, engine_selection=engine_selection)
    cleanup_factory.assert_called_once_with(step=step, engine_selection=engine_selection)


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [_engine_selection(), None])
async def test_upload_resolves_before_dispatch_and_reuses_exact_selection(selection: object | None) -> None:
    events: list[str] = []
    locator = MagicMock(set_input_files=AsyncMock(side_effect=lambda *args, **kwargs: events.append("dispatch")))
    element = MagicMock(locator=locator, is_file_input=AsyncMock(return_value=True))
    dom = MagicMock(get_skyvern_element_by_id=AsyncMock(return_value=element))
    task = MagicMock(navigation_goal="https://example.test/file", navigation_payload={}, organization_id="org")
    resolver = Mock(side_effect=lambda *args: events.append("resolve") or selection)
    wait = AsyncMock(side_effect=lambda *args, **kwargs: events.append("wait"))
    with (
        patch.object(handler_module, "DomUtil", return_value=dom),
        patch.object(
            handler_module, "get_actual_value_of_parameter_if_secret_with_task", side_effect=lambda _, url: url
        ),
        patch.object(handler_module.handler_utils, "download_file", AsyncMock(return_value="/tmp/file")),
        patch.object(handler_module.SkyvernElement, "wait_until_enabled", AsyncMock(return_value=True)),
        patch.object(handler_module, "resolve_engine_selection_for_task", resolver),
        patch.object(handler_module, "_wait_for_upload_processing", wait),
    ):
        await handler_module.handle_upload_file_action(
            UploadFileAction(element_id="file", file_url="https://example.test/file"),
            MagicMock(),
            MagicMock(),
            task,
            MagicMock(),
        )
    assert events == ["resolve", "dispatch", "wait"]
    resolver.assert_called_once_with(task, handler_module.app.BROWSER_MANAGER)
    assert wait.await_args.kwargs["engine_selection"] is selection


@pytest.mark.asyncio
@pytest.mark.parametrize("local_path", [False, True], ids=["remote-fetch", "local-path-permission"])
async def test_upload_fetch_failure_does_not_fall_through_to_click_success(local_path: bool) -> None:
    file_url = "/outside/example.txt" if local_path else "https://files.example.test/example.txt"
    element = MagicMock(
        locator=MagicMock(),
        is_file_input=AsyncMock(return_value=False),
        find_file_input_in_children=AsyncMock(return_value=None),
    )
    dom = MagicMock(get_skyvern_element_by_id=AsyncMock(return_value=element))
    task = MagicMock(navigation_goal=file_url, navigation_payload={}, organization_id="org")
    click_fallback = AsyncMock(return_value=[ActionSuccess()])
    page = MagicMock()
    listener = Mock()
    context = handler_module.skyvern_context.SkyvernContext(
        pending_file_chooser=handler_module.skyvern_context.PendingFileChooserListener(
            page=page,
            file_paths="/tmp/previous.txt",
            handler=listener,
        )
    )

    with (
        patch.object(handler_module, "DomUtil", return_value=dom),
        patch.object(handler_module, "get_actual_value_of_parameter_if_secret_with_task", return_value=file_url),
        patch.object(
            handler_module.handler_utils,
            "download_file_api",
            new=AsyncMock(
                side_effect=AssertionError("local paths must not use the remote fetch")
                if local_path
                else RuntimeError("fetch failed")
            ),
        ),
        patch.object(handler_module.handler_utils, "resolve_run_download_id", return_value="wr_test"),
        patch.object(
            handler_module.handler_utils,
            "validate_local_file_path",
            side_effect=PermissionError("outside the allowed download directory")
            if local_path
            else AssertionError("remote URLs must not use local path validation"),
        ),
        patch.object(handler_module.SkyvernElement, "wait_until_enabled", new=AsyncMock(return_value=True)),
        patch.object(handler_module, "chain_click", new=click_fallback),
        patch.object(handler_module.skyvern_context, "current", return_value=context),
    ):
        results = await handler_module.handle_upload_file_action(
            UploadFileAction(element_id="file", file_url=file_url),
            page,
            MagicMock(),
            task,
            MagicMock(),
        )

    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert "Failed to download file" in results[0].exception_message
    click_fallback.assert_not_awaited()
    assert context.pending_file_chooser is None
    page.remove_listener.assert_called_once_with("filechooser", listener)


@pytest.mark.asyncio
async def test_click_upload_fetch_failure_returns_action_failure() -> None:
    file_url = "https://files.example.test/example.txt"
    element = MagicMock(get_id=Mock(return_value="file"), locator=MagicMock())
    dom = MagicMock(resolve_effective_click_target=AsyncMock(return_value=element))
    task = MagicMock(organization_id="org")
    page = MagicMock()
    listener = Mock()
    context = handler_module.skyvern_context.SkyvernContext(
        pending_file_chooser=handler_module.skyvern_context.PendingFileChooserListener(
            page=page,
            file_paths="/tmp/previous.txt",
            handler=listener,
        )
    )

    with (
        patch.object(handler_module, "DomUtil", return_value=dom),
        patch.object(handler_module, "get_actual_value_of_parameter_if_secret_with_task", return_value=file_url),
        patch.object(
            handler_module.handler_utils,
            "download_file_api",
            new=AsyncMock(side_effect=RuntimeError("fetch failed")),
        ),
        patch.object(handler_module.skyvern_context, "current", return_value=context),
    ):
        results = await handler_module.chain_click(
            task,
            MagicMock(),
            page,
            ClickAction(element_id="file", file_url=file_url),
            element,
        )

    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_message == f"Failed to download file from {file_url}: File download failed"
    assert context.pending_file_chooser is None
    page.remove_listener.assert_called_once_with("filechooser", listener)


@pytest.mark.asyncio
async def test_swallows_playwright_timeout() -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = PlaywrightTimeoutError("Timeout 3000ms exceeded")
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_swallows_asyncio_timeout() -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = asyncio.TimeoutError()
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_swallows_page_analysis_timeout_during_frame_creation() -> None:
    with patch(
        "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
        new_callable=AsyncMock,
        side_effect=SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page"),
    ):
        await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_swallows_playwright_error() -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = PlaywrightError("Target page, context or browser has been closed")
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_propagates_non_playwright_error() -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = RuntimeError("unexpected bug")
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        with pytest.raises(RuntimeError, match="unexpected bug"):
            await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_swallows_selected_engine_timeout() -> None:
    # A non-stock engine's native timeout must be tolerated like the stock Playwright timeout.
    await _run_with_page_ready_error(_EngineTimeout("deadline exceeded"), _engine_selection())


@pytest.mark.asyncio
async def test_swallows_selected_engine_error() -> None:
    await _run_with_page_ready_error(_EngineError("target closed"), _engine_selection())


@pytest.mark.asyncio
async def test_propagates_foreign_playwright_error_under_selected_engine() -> None:
    # Under a pinned non-stock engine, a stock Playwright error is foreign and must propagate,
    # not be swallowed as an upload-processing tolerance.
    with pytest.raises(PlaywrightError):
        await _run_with_page_ready_error(PlaywrightError("navigated away"), _engine_selection())


@pytest.mark.asyncio
async def test_propagates_foreign_playwright_timeout_under_selected_engine() -> None:
    with pytest.raises(PlaywrightTimeoutError):
        await _run_with_page_ready_error(PlaywrightTimeoutError("pw timeout"), _engine_selection())


# ---------------------------------------------------------------------------
# Static regression guards
# ---------------------------------------------------------------------------


def _get_all_sleep_calls(source: str) -> list[tuple[int, float | str]]:
    results = []
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        is_sleep = (
            isinstance(func, ast.Attribute)
            and func.attr == "sleep"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
        if is_sleep and call.args:
            arg = call.args[0]
            if isinstance(arg, ast.Constant):
                results.append((node.lineno, arg.value))
    return results


def test_no_fixed_sleep_10_in_handle_upload_file_action() -> None:
    # Regression guard: this function previously blocked the agent for 10s unconditionally.
    source = inspect.getsource(handler_module.handle_upload_file_action)
    for lineno, value in _get_all_sleep_calls(source):
        assert value != 10, f"Found asyncio.sleep(10) at line {lineno}"


def test_no_fixed_sleep_15_in_chain_click() -> None:
    # Regression guard: this function previously blocked the agent for 15s unconditionally.
    source = inspect.getsource(handler_module.chain_click)
    for lineno, value in _get_all_sleep_calls(source):
        assert value != 15, f"Found asyncio.sleep(15) at line {lineno}"
