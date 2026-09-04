import asyncio
import contextlib
import io
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import structlog
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, ValidationError

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOAD_TIMEOUT
from skyvern.errors.errors import UserDefinedError
from skyvern.exceptions import BlockedHost
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import (
    ActionStatus,
    ClickAction,
    DownloadFileAction,
    SelectOption,
    SelectOptionAction,
)
from skyvern.webeye.actions.handler import (
    _BLOCKED_INLINE_PDF_RECOVERY_TIMEOUT_SECONDS,
    _INLINE_IFRAME_SRC_JS,
    DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE,
    ActionHandler,
    ScopedXhrDownloadCapture,
    _cleanup_captured_download_popup,
    _collect_inline_iframe_src_candidates,
    _EagerAdoptedBlobCapture,
    _looks_like_pdf,
    _persist_captured_download,
    _recover_adopted_session_blob_pdf_iframe,
    _recover_blocked_inline_pdf_download,
    _remove_download_listener,
    handle_download_file_action,
)
from skyvern.webeye.actions.responses import ActionAbort, ActionFailure, ActionSuccess
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from skyvern.webeye.utils.page import BlobActionFreshness
from tests.unit.helpers import make_organization, make_step, make_task

# Five seconds is only a test-side runaway guard. The behavior under test is
# asserted through configured timeout values, cleanup, and span attributes
# below; it tolerates the scheduling delay a loaded CI runner can add to a
# completed coroutine without turning scheduling latency into a product failure.
CI_TEST_RUNAWAY_TIMEOUT_SECONDS = 5.0


def _bind_adopted_download_authorizer(
    page: MagicMock,
    authorize_request_hop: object,
    *,
    owner_context: object | None = None,
) -> None:
    interceptor = MagicMock()
    interceptor._page_context = page.context if owner_context is None else owner_context
    interceptor._redirect_hop_authorizer = authorize_request_hop
    interceptor.download_scope = None
    interceptor._cookie_header_for_url = AsyncMock(return_value="session=authenticated")
    page.context._skyvern_cdp_download_interceptor_bind_lock = asyncio.Lock()
    page.context._skyvern_cdp_download_interceptor = interceptor
    page.is_closed.return_value = False


async def _assert_background_tasks_drained(tasks: set[asyncio.Task[None]]) -> None:
    if tasks:
        await asyncio.wait(tuple(tasks), timeout=0.25)
        await asyncio.sleep(0)
    assert not tasks


class _EventEmitter:
    def __init__(self, context: object = None, url: str = "https://example.test/files") -> None:
        self.listeners: dict[str, list[Callable]] = {}
        self.context, self.url = context, url

    def on(self, event: str, callback: Callable) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Callable) -> None:
        if callback in (callbacks := self.listeners.get(event, [])):
            callbacks.remove(callback)

    off = remove_listener

    def emit(self, event: str, value: object) -> object:
        for callback in list(self.listeners.get(event, [])):
            callback(value)
        return value


def _download(*, path: Path | None = None, failure: str | None = None, save_as: object = None) -> MagicMock:
    download = MagicMock(suggested_filename=path.name if path else "download.pdf")
    download.failure = AsyncMock(return_value=failure)
    download.path = AsyncMock(return_value=path)
    download.save_as = AsyncMock(side_effect=save_as)
    return download


def test_download_not_triggered_message_claims_only_observation() -> None:
    # download_triggered=false proves only that Skyvern did not observe/credit a
    # download after the action — NOT categorically that no download started or no
    # file was saved (late/missed artifacts are possible). Pin the exact intended
    # observation-only string so no categorical save/start wording can creep back in.
    assert DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE == (
        "No file download was observed or credited after this action. "
        "If the goal still requires this file, keep trying to download it rather than reporting the goal complete."
    )
    lowered = DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE.lower()
    assert "not saved" not in lowered
    assert "download started" not in lowered


@pytest.mark.asyncio
async def test_persist_captured_download_cancellation_cleans_owned_target(tmp_path: Path) -> None:
    async def save_as(target: Path) -> None:
        Path(target).touch()
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _persist_captured_download(
            _download(save_as=save_as), target=(target := tmp_path / "partial.pdf"), timeout=1
        )
    assert not target.exists()


def _make_false_click_observation_context() -> tuple:
    now = datetime.now(UTC)
    task = make_task(
        now, make_organization(now), workflow_run_id="wr-popup", browser_session_id="bs-popup", download_timeout=0.05
    )
    step = make_step(now, task, step_id="step-popup", status=StepStatus.created, order=0, output=None)
    page = _EventEmitter(context := _EventEmitter())
    scraped_page = MagicMock(_browser_state=MagicMock())
    scraped_page._browser_state.list_valid_pages = AsyncMock(return_value=[page])
    return task, step, context, page, scraped_page, ClickAction(element_id="download-link", download=False)


async def _run_false_click_observation(
    tmp_path: Path,
    *,
    click_effect: Callable[[_EventEmitter, _EventEmitter], object] | None = None,
    remote: bool = False,
    needs_cdp_frame_publisher: bool = False,
    rig: tuple | None = None,
    action_outcome: list[ActionSuccess | ActionFailure] | BaseException | None = None,
    grace: float = 0.05,
    get_download_dir_mock: MagicMock | None = None,
) -> tuple:
    task, step, context, page, scraped_page, action = rig or _make_false_click_observation_context()
    scraped_page._browser_state.release_driver_on_close = remote
    scraped_page._browser_state.browser_artifacts.needs_cdp_frame_publisher = needs_cdp_frame_publisher
    app_mock = MagicMock()
    storage = app_mock.STORAGE
    storage.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])
    storage.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
    app_mock.BROWSER_MANAGER.get_for_task.return_value = scraped_page._browser_state
    app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)

    async def inner(*args: object, **kwargs: object) -> list[ActionSuccess | ActionFailure]:
        if click_effect:
            click_effect(context, page)
        if isinstance(action_outcome, BaseException):
            raise action_outcome
        if action_outcome is not None:
            return action_outcome
        return [ActionSuccess()]

    gdd_mock = get_download_dir_mock if get_download_dir_mock is not None else MagicMock(return_value=str(tmp_path))
    with (
        patch.object(ActionHandler, "_handle_action", side_effect=inner),
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.get_download_dir", gdd_mock),
        patch("skyvern.webeye.actions.handler.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", grace),
    ):
        results = await ActionHandler.handle_action(
            scraped_page,
            task,
            step,
            page,
            action,
            file_download_false_click_eligible=True,
        )
    return results, action, context, page, storage


@pytest.mark.asyncio
async def test_reused_action_with_stale_finished_at_gets_fresh_stamp_on_exception(
    span_exporter: InMemorySpanExporter,
) -> None:
    """A hydrated/cached action arrives with a prior finished_at; if this execution's inner
    action raises, the fallback must stamp THIS execution, never preserve the stale value
    (which would yield finished_at < started_at)."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    stale = datetime(2020, 1, 1, 0, 0, 0)
    action.finished_at = stale

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=RuntimeError("boom")),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            with pytest.raises(RuntimeError):
                await ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )

    assert action.started_at is not None
    assert action.finished_at is not None
    assert action.finished_at != stale
    assert action.started_at <= action.finished_at


@pytest.mark.asyncio
async def test_agent_action_persists_execution_window(tmp_path: Path) -> None:
    rig = _make_false_click_observation_context()
    rig[-1].status = ActionStatus.completed

    _, action, _, _, _ = await _run_false_click_observation(tmp_path, rig=rig)

    assert action.started_at is not None
    assert action.finished_at is not None
    assert action.started_at <= action.finished_at


@pytest.mark.asyncio
async def test_false_click_captured_download_is_finalized_after_action_failure(tmp_path: Path) -> None:
    downloaded_path = tmp_path / "captured-after-failure.pdf"
    downloaded_path.write_bytes(b"content")
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, action = rig
    unrelated = _EventEmitter(context, "https://example.test/unrelated")
    popup = _EventEmitter(context, "about:blank")
    unrelated.close = AsyncMock()  # type: ignore[attr-defined]
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()
    failure = ActionFailure(RuntimeError("click failed"))

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(path=downloaded_path))
        clicked_page.url = "about:blank"

    with patch(
        "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
        new=AsyncMock(),
    ) as settle:
        results, _, _, _, _ = await _run_false_click_observation(
            tmp_path, click_effect=click, rig=rig, action_outcome=[failure]
        )

    assert results == [failure]
    assert results[0] is failure and not results[0].success
    assert results[0].downloaded_files == action.downloaded_files == ["captured-after-failure.pdf"]
    assert results[0].download_triggered is action.download_triggered is True
    settle.assert_awaited_once()
    popup.close.assert_awaited_once()
    unrelated.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")


@pytest.mark.asyncio
@pytest.mark.parametrize("post_settle", ["empty", "vanished"])
async def test_false_click_action_failure_does_not_credit_disqualified_artifact(
    tmp_path: Path, post_settle: str
) -> None:
    downloaded_path = tmp_path / "disqualified-after-failure.pdf"
    downloaded_path.write_bytes(b"content")
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, action = rig
    unrelated = _EventEmitter(context, "https://example.test/unrelated")
    popup = _EventEmitter(context, "about:blank")
    unrelated.close = AsyncMock()  # type: ignore[attr-defined]
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()
    failure = ActionFailure(RuntimeError("click failed"))

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(path=downloaded_path))
        clicked_page.url = "about:blank"

    async def disqualify(**_: object) -> None:
        if post_settle == "empty":
            downloaded_path.write_bytes(b"")
        else:
            downloaded_path.unlink()

    with patch(
        "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
        new=AsyncMock(side_effect=disqualify),
    ):
        results, _, _, _, _ = await _run_false_click_observation(
            tmp_path, click_effect=click, rig=rig, action_outcome=[failure]
        )

    assert results == [failure]
    assert results[0] is failure and not results[0].success
    assert not results[0].download_triggered and not results[0].downloaded_files
    assert not action.download_triggered and not action.downloaded_files
    popup.close.assert_awaited_once()
    unrelated.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")


@pytest.mark.asyncio
async def test_false_click_captured_download_is_finalized_before_original_exception(tmp_path: Path) -> None:
    downloaded_path = tmp_path / "captured-before-exception.pdf"
    downloaded_path.write_bytes(b"content")
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, action = rig
    unrelated = _EventEmitter(context, "https://example.test/unrelated")
    popup = _EventEmitter(context, "about:blank")
    unrelated.close = AsyncMock()  # type: ignore[attr-defined]
    popup.close = AsyncMock(side_effect=RuntimeError("cleanup failed"))  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()
    original = RuntimeError("original click exception")

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(path=downloaded_path))
        clicked_page.url = "about:blank"

    with (
        patch(
            "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
            new=AsyncMock(),
        ) as settle,
        pytest.raises(RuntimeError, match="original click exception") as raised,
    ):
        await _run_false_click_observation(tmp_path, click_effect=click, rig=rig, action_outcome=original)

    assert raised.value is original
    assert not action.download_triggered and not action.downloaded_files
    settle.assert_awaited_once()
    popup.close.assert_awaited_once()
    unrelated.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")


@pytest.mark.asyncio
async def test_false_click_listener_cleanup_does_not_mask_original_exception(tmp_path: Path) -> None:
    downloaded_path = tmp_path / "captured-before-listener-cleanup.pdf"
    downloaded_path.write_bytes(b"content")
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, action = rig
    popup = _EventEmitter(context, "about:blank")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()
    original = RuntimeError("original click exception")

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(path=downloaded_path))
        clicked_page.off = MagicMock(side_effect=RuntimeError("popup listener removal failed"))  # type: ignore[method-assign]

    with (
        patch(
            "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
            new=AsyncMock(),
        ),
        pytest.raises(RuntimeError, match="original click exception") as raised,
    ):
        await _run_false_click_observation(tmp_path, click_effect=click, rig=rig, action_outcome=original)

    assert raised.value is original
    assert popup.listeners["download"] == []
    popup.close.assert_awaited_once()
    assert not action.download_triggered and not action.downloaded_files


@pytest.mark.asyncio
async def test_false_click_finalizes_artifacts_and_closes_only_emitting_popup(tmp_path: Path) -> None:
    _, _, context, page, storage = await _run_false_click_observation(tmp_path)
    assert context.listeners == {} and not page.listeners["popup"] and storage.mock_calls == []
    (downloaded_path := tmp_path / "captured_1.pdf").write_bytes(b"content")
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    unrelated = _EventEmitter(context, "https://example.test/unrelated")
    popup = _EventEmitter(context, "about:blank")
    unrelated.close = AsyncMock()  # type: ignore[attr-defined]
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(path=downloaded_path))
        clicked_page.url = "about:blank"

    with patch(
        "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
        new=AsyncMock(),
    ):
        results, action, _, _, _ = await _run_false_click_observation(tmp_path, click_effect=click, rig=rig)

    popup.close.assert_awaited_once()
    unrelated.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")
    assert results[-1].downloaded_files == action.downloaded_files == ["captured_1.pdf"]


@pytest.mark.asyncio
async def test_false_click_grace_zero_closes_download_popup_without_persisting(tmp_path: Path) -> None:
    # At grace=0 a FileDownloadBlock click that mints a download on a popup must still have that popup
    # closed and the original page restored before handle_action returns; persistence stays gated on
    # grace>0, so the download is not credited here.
    downloaded_path = tmp_path / "captured_grace0.pdf"
    downloaded_path.write_bytes(b"content")
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, action = rig
    popup = _EventEmitter(context, ":")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(path=downloaded_path))
        clicked_page.url = "about:blank"

    results, action, _, _, _ = await _run_false_click_observation(tmp_path, click_effect=click, rig=rig, grace=0)

    popup.close.assert_awaited_once()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")
    assert not action.download_triggered and not action.downloaded_files
    assert not results[-1].download_triggered


@pytest.mark.asyncio
async def test_false_click_grace_zero_leaves_non_download_popup_open(tmp_path: Path) -> None:
    # A popup event without a confirmed same-action download must never be closed as a download popup.
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, action = rig
    popup = _EventEmitter(context, ":")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)  # popup opens, but no download is confirmed

    results, action, _, _, _ = await _run_false_click_observation(tmp_path, click_effect=click, rig=rig, grace=0)

    popup.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_not_awaited()
    assert not action.download_triggered and not action.downloaded_files


@pytest.mark.asyncio
async def test_false_click_grace_zero_adds_no_capture_dwell(tmp_path: Path) -> None:
    # At grace=0 the capture path adds no new timed wait -- it acts only on an already-resolved
    # download event. The admission wait_for lives in _handle_action (patched out here), so a zero
    # count here is scoped to the outer capture, not a global wait_for ban.
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    popup = _EventEmitter(context, ":")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)  # popup, no download -> event never resolves

    wait_for_spy = AsyncMock(wraps=asyncio.wait_for)
    with patch("skyvern.webeye.actions.handler.asyncio.wait_for", wait_for_spy):
        await _run_false_click_observation(tmp_path, click_effect=click, rig=rig, grace=0)

    wait_for_spy.assert_not_awaited()
    popup.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_false_click_grace_zero_closes_only_download_popup(tmp_path: Path) -> None:
    # Only the popup that minted the download is closed; a pre-existing tab and the original action
    # page are never closed.
    downloaded_path = tmp_path / "captured_iso.pdf"
    downloaded_path.write_bytes(b"content")
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    unrelated = _EventEmitter(context, "https://example.test/unrelated")
    popup = _EventEmitter(context, ":")
    unrelated.close = AsyncMock()  # type: ignore[attr-defined]
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    page.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(path=downloaded_path))
        clicked_page.url = "about:blank"

    await _run_false_click_observation(tmp_path, click_effect=click, rig=rig, grace=0)

    popup.close.assert_awaited_once()
    unrelated.close.assert_not_awaited()
    page.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_false_click_grace_zero_runs_no_persistence_setup(tmp_path: Path) -> None:
    # At grace=0 no persistence-only setup may run -- get_download_dir must not be called and no
    # storage listing happens -- while the captured popup is still closed and the original page
    # restored.
    downloaded_path = tmp_path / "captured_no_setup.pdf"
    downloaded_path.write_bytes(b"content")
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    popup = _EventEmitter(context, ":")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()
    get_download_dir_mock = MagicMock(return_value=str(tmp_path))

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(path=downloaded_path))
        clicked_page.url = "about:blank"

    _, action, _, _, storage = await _run_false_click_observation(
        tmp_path, click_effect=click, rig=rig, grace=0, get_download_dir_mock=get_download_dir_mock
    )

    get_download_dir_mock.assert_not_called()
    storage.list_downloaded_files_in_browser_session.assert_not_called()
    popup.close.assert_awaited_once()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")
    assert not action.download_triggered and not action.downloaded_files


@pytest.mark.asyncio
@pytest.mark.parametrize("eager", [None, "remote"])
async def test_false_click_post_settle_empty_artifact_is_uncredited(tmp_path: Path, eager: str | None) -> None:
    local_path = tmp_path / "local.pdf" if eager is None else None
    if local_path is not None:
        local_path.write_bytes(b"content")

    async def save_as(target: Path) -> None:
        Path(target).write_bytes(b"content")

    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, action = rig
    unrelated = _EventEmitter(context, "https://example.test/unrelated")
    popup = _EventEmitter(context, "about:blank")
    unrelated.close = AsyncMock()  # type: ignore[attr-defined]
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(path=local_path, save_as=save_as))
        clicked_page.url = "about:blank"

    async def truncate_persisted_artifact(**_: object) -> None:
        persisted_path = local_path or next(tmp_path.iterdir())
        persisted_path.write_bytes(b"")

    with patch(
        "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
        new=AsyncMock(side_effect=truncate_persisted_artifact),
    ):
        results, _, context, page, _ = await _run_false_click_observation(
            tmp_path,
            click_effect=click,
            remote=eager == "remote",
            rig=rig,
        )

    assert not results[-1].download_triggered and not results[-1].downloaded_files
    assert not action.download_triggered and not action.downloaded_files
    assert page.listeners["popup"] == popup.listeners["download"] == []
    popup.close.assert_awaited_once()
    unrelated.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "eager"),
    [
        ("saved", "remote"),
        ("local_path", None),
        ("empty", "cdp"),
        ("download_failed", "remote"),
        ("path_unavailable", None),
        ("timeout", "remote"),
        ("save_failed", "remote"),
    ],
)
async def test_false_click_unsuccessful_download_is_uncredited(tmp_path: Path, outcome: str, eager: str | None) -> None:
    async def save_as(target: Path) -> None:
        if outcome == "timeout":
            await asyncio.Event().wait()
        if outcome == "save_failed":
            raise RuntimeError("save failed")
        Path(target).write_bytes(b"content" if outcome == "saved" else b"")

    local_path = tmp_path / "local.pdf" if outcome == "local_path" else None
    if local_path is not None:
        local_path.write_bytes(b"content")
    download = _download(
        path=local_path,
        failure="failed" if outcome == "download_failed" else None,
        save_as=save_as,
    )
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    unrelated = _EventEmitter(context, "https://example.test/unrelated")
    popup = _EventEmitter(context, "about:blank")
    unrelated.close = AsyncMock()  # type: ignore[attr-defined]
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", download)
        clicked_page.url = "about:blank"

    with patch(
        "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
        new=AsyncMock(side_effect=lambda **_: next(tmp_path.iterdir()).unlink()),
    ) as settle:
        results, _, context, page, storage = await _run_false_click_observation(
            tmp_path,
            click_effect=click,
            remote=eager == "remote",
            needs_cdp_frame_publisher=eager == "cdp",
            rig=rig,
        )
    assert not results[-1].download_triggered and not results[-1].downloaded_files and not list(tmp_path.iterdir())
    assert page.listeners["popup"] == popup.listeners["download"] == []
    assert settle.await_count == (1 if outcome in {"saved", "local_path"} else 0)
    popup.close.assert_awaited_once()
    unrelated.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")
    assert storage.list_downloaded_files_in_browser_session.await_count == (
        2 if outcome in {"saved", "local_path"} else 0
    )


@pytest.mark.asyncio
async def test_false_click_persistence_cancellation_cleans_popup_and_propagates(tmp_path: Path) -> None:
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    unrelated = _EventEmitter(context, "https://example.test/unrelated")
    popup = _EventEmitter(context, "about:blank")
    unrelated.close = AsyncMock()  # type: ignore[attr-defined]
    popup.close = AsyncMock(side_effect=RuntimeError("cleanup close failed"))  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(save_as=asyncio.CancelledError()))
        clicked_page.url = "about:blank"

    with pytest.raises(asyncio.CancelledError):
        await _run_false_click_observation(tmp_path, click_effect=click, remote=True, rig=rig)

    assert not rig[-1].download_triggered and not rig[-1].downloaded_files
    popup.close.assert_awaited_once()
    unrelated.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")


@pytest.mark.asyncio
async def test_false_click_persistence_can_exceed_grace_within_task_timeout(tmp_path: Path) -> None:
    rig = _make_false_click_observation_context()
    task, _, context, page, scraped_page, action = rig
    task.download_timeout = 10
    popup = _EventEmitter(context, "about:blank")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()
    persistence_timeouts: list[float] = []

    async def persist_with_task_timeout(
        _download: object, *, target: Path, timeout: float, owned_dir: Path
    ) -> MagicMock:
        persistence_timeouts.append(timeout)
        Path(target).write_bytes(b"content")
        return MagicMock(path=target, outcome="saved")

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download())

    with (
        patch(
            "skyvern.webeye.actions.handler._persist_captured_download",
            new=AsyncMock(side_effect=persist_with_task_timeout),
        ),
        patch(
            "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
            new=AsyncMock(),
        ),
    ):
        results, _, _, _, _ = await _run_false_click_observation(tmp_path, click_effect=click, remote=True, rig=rig)

    assert persistence_timeouts == [10]
    assert results[-1].downloaded_files == action.downloaded_files
    assert len(action.downloaded_files) == 1 and action.downloaded_files[0].endswith("-download.pdf")
    assert results[-1].download_triggered is action.download_triggered is True
    assert page.listeners["popup"] == popup.listeners["download"] == []
    popup.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_false_click_persistence_remains_bounded_by_task_timeout(tmp_path: Path) -> None:
    rig = _make_false_click_observation_context()
    task, _, context, page, scraped_page, action = rig
    task.download_timeout = 0.01
    popup = _EventEmitter(context, "about:blank")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    async def save_as(_target: Path) -> None:
        await asyncio.Event().wait()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(save_as=save_as))

    results, _, _, _, _ = await _run_false_click_observation(tmp_path, click_effect=click, remote=True, rig=rig)

    assert not results[-1].download_triggered and not action.download_triggered
    assert not list(tmp_path.iterdir())
    assert page.listeners["popup"] == popup.listeners["download"] == []
    popup.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_false_click_processing_cancellation_after_click_exception_propagates(tmp_path: Path) -> None:
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    popup = _EventEmitter(context, "about:blank")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()
    original = RuntimeError("original click exception")

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(save_as=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await _run_false_click_observation(tmp_path, click_effect=click, remote=True, rig=rig, action_outcome=original)

    assert page.listeners["popup"] == popup.listeners["download"] == []
    popup.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_false_click_cancellation_from_click_skips_captured_download_processing(tmp_path: Path) -> None:
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    popup = _EventEmitter(context, "about:blank")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()
    original = asyncio.CancelledError()

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download())

    with (
        patch("skyvern.webeye.actions.handler._persist_captured_download", new=AsyncMock()) as persist,
        patch(
            "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
            new=AsyncMock(),
        ) as settle,
        pytest.raises(asyncio.CancelledError) as raised,
    ):
        await _run_false_click_observation(tmp_path, click_effect=click, rig=rig, action_outcome=original)

    assert raised.value is original
    persist.assert_not_awaited()
    settle.assert_not_awaited()
    assert page.listeners["popup"] == popup.listeners["download"] == []
    popup.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_false_click_processing_failure_after_click_exception_preserves_original(tmp_path: Path) -> None:
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    popup = _EventEmitter(context, "about:blank")
    popup.close = AsyncMock()  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()
    original = RuntimeError("original click exception")

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download())

    with (
        patch(
            "skyvern.webeye.actions.handler._persist_captured_download",
            new=AsyncMock(side_effect=ValueError("processing failed")),
        ),
        pytest.raises(RuntimeError, match="original click exception") as raised,
    ):
        await _run_false_click_observation(tmp_path, click_effect=click, remote=True, rig=rig, action_outcome=original)

    assert raised.value is original
    assert page.listeners["popup"] == popup.listeners["download"] == []
    popup.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_operations", "expected_log_count"),
    [
        ({"popup_close"}, 1),
        ({"working_page_recovery"}, 1),
        ({"popup_close", "working_page_recovery"}, 2),
    ],
)
async def test_cleanup_captured_download_popup_logs_failures_and_remains_best_effort(
    failed_operations: set[str], expected_log_count: int
) -> None:
    page = _EventEmitter(url="about:blank")
    popup = _EventEmitter(url="about:blank")
    popup.close = AsyncMock(side_effect=RuntimeError("close failed") if "popup_close" in failed_operations else None)  # type: ignore[attr-defined]
    browser_state = MagicMock()
    browser_state.navigate_to_url = AsyncMock(
        side_effect=RuntimeError("navigate failed") if "working_page_recovery" in failed_operations else None
    )

    with patch("skyvern.webeye.actions.handler.LOG.warning") as warning:
        await _cleanup_captured_download_popup(popup, browser_state, page, "https://example.test/files")

    popup.close.assert_awaited_once()
    browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")
    assert warning.call_count == expected_log_count
    assert {call.kwargs["operation"] for call in warning.call_args_list} == failed_operations
    assert {call.kwargs["exception_type"] for call in warning.call_args_list} == {"RuntimeError"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("settle failed"), asyncio.CancelledError()])
async def test_false_click_finalization_failure_cleans_popup_and_propagates(
    tmp_path: Path, failure: BaseException
) -> None:
    rig = _make_false_click_observation_context()
    _, _, context, page, scraped_page, _ = rig
    unrelated = _EventEmitter(context, "https://example.test/unrelated")
    popup = _EventEmitter(context, "about:blank")
    unrelated.close = AsyncMock()  # type: ignore[attr-defined]
    popup.close = AsyncMock(side_effect=RuntimeError("cleanup close failed"))  # type: ignore[attr-defined]
    scraped_page._browser_state.navigate_to_url = AsyncMock()

    async def save_as(target: Path) -> None:
        Path(target).write_bytes(b"content")

    def click(_context: _EventEmitter, clicked_page: _EventEmitter) -> None:
        clicked_page.emit("popup", popup)
        popup.emit("download", _download(save_as=save_as))
        clicked_page.url = "about:blank"

    with (
        patch(
            "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
            new=AsyncMock(side_effect=failure),
        ),
        pytest.raises(type(failure), match="settle failed" if isinstance(failure, RuntimeError) else None),
    ):
        await _run_false_click_observation(tmp_path, click_effect=click, remote=True, rig=rig)

    popup.close.assert_awaited_once()
    unrelated.close.assert_not_awaited()
    scraped_page._browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.test/files")


def _download_wait_span_attrs(span_exporter: InMemorySpanExporter) -> dict:
    span = next(
        (span for span in span_exporter.get_finished_spans() if span.name == "skyvern.agent.action.download_wait"),
        None,
    )
    assert span is not None, "expected download_wait span to be recorded"
    return dict(span.attributes or {})


class _FakeMonotonic:
    def __init__(self) -> None:
        self.current = 0.0
        self._advance_after_next_read: float | None = None

    def advance_after_next_read(self, current: float) -> None:
        self._advance_after_next_read = current

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)

    def monotonic(self) -> float:
        current = self.current
        if self._advance_after_next_read is not None:
            self.current = self._advance_after_next_read
            self._advance_after_next_read = None
        return current


def _make_download_click_context(
    *,
    now: datetime,
    organization,
    page_url: str,
    task_overrides: dict | None = None,
) -> tuple:
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
        download_timeout=30.0,
        **(task_overrides or {}),
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)
    page = MagicMock()
    page.url = page_url
    page.is_closed.return_value = False
    page.context.browser = None
    browser_state = MagicMock()
    # A real browser attaches no vendor download source unless a cloud factory did; mirror that default
    # so the provider-observation seam stays inert for the local/PBS/event paths these cases exercise.
    browser_state.browser_artifacts.get_action_download_source.return_value = None
    browser_state.list_valid_pages = AsyncMock(return_value=[page])
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )
    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )
    return task, step, page, browser_state, scraped_page, action


@pytest.mark.asyncio
async def test_handle_action_recovers_working_page_closed_by_download_click() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/downloads",
        task_overrides={
            "error_code_mapping": {
                "download_failed": "The requested download could not be prepared",
            },
        },
    )
    task = task.model_copy(update={"download_timeout": 0.01})
    transient_callbacks: dict[str, Callable] = {}
    unrelated_page = MagicMock()
    unrelated_page.url = "https://example.com/unrelated"
    recovered_page = MagicMock()
    recovered_page.url = page.url
    recovered_page.is_closed.return_value = False
    recovered_page.context = page.context
    page.context._skyvern_cdp_download_active = False

    async def expose_original_binding(_name: str, callback: Callable) -> None:
        transient_callbacks["original"] = callback

    page.expose_binding = AsyncMock(side_effect=expose_original_binding)
    page.evaluate = AsyncMock(return_value=[])
    recovered_page.expose_binding = AsyncMock()
    recovered_page.evaluate = AsyncMock(return_value=[])
    browser_state.list_valid_pages = AsyncMock(return_value=[page, unrelated_page])
    browser_state.new_page = AsyncMock(return_value=recovered_page)
    browser_state.navigate_to_url = AsyncMock()
    browser_state.set_active_page = AsyncMock()

    async def close_page_during_click(*args: object, **kwargs: object) -> list[ActionSuccess]:
        transient_callbacks["original"](
            {},
            {"text": "The requested download could not be prepared", "timestamp_ms": 1},
        )
        page.is_closed.return_value = True
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=close_page_during_click),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    browser_state.new_page.assert_awaited_once_with()
    browser_state.navigate_to_url.assert_awaited_once_with(
        page=recovered_page,
        url="https://example.com/downloads",
    )
    browser_state.set_active_page.assert_awaited_once_with(recovered_page)
    assert unrelated_page is not recovered_page
    assert results[-1].download_triggered is False
    assert results[-1].skip_remaining_actions is True
    assert isinstance(results[-1], ActionFailure)
    assert [error.error_code for error in action.errors or []] == ["download_failed"]


@pytest.mark.asyncio
# The driver often has not observed the browser close yet when the pending newPage call rejects,
# so is_connected() still answers True on the production path this recovery exists for.
@pytest.mark.parametrize("driver_reports_connected", [False, True])
async def test_handle_action_reconnects_context_closed_during_download_wait(
    driver_reports_connected: bool,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/downloads",
    )
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])
    page.context._skyvern_cdp_download_active = False
    recovered_page = MagicMock(url=page.url, context=MagicMock())
    recovered_page.is_closed.return_value = False
    page_closed = False
    context_reconnected = False
    page.is_closed.side_effect = lambda: page_closed
    browser_state.browser_artifacts.applied_browser_profile_id = None
    browser_state.is_connected.return_value = driver_reports_connected
    browser_state.new_page = AsyncMock(side_effect=RuntimeError("Target page, context or browser has been closed"))

    async def reconnect_context(**_kwargs: object) -> None:
        nonlocal context_reconnected
        context_reconnected = True

    async def get_working_page() -> object | None:
        if context_reconnected:
            return recovered_page
        return None if page_closed else page

    browser_state.reconnect = AsyncMock(side_effect=reconnect_context)
    browser_state.get_working_page = AsyncMock(side_effect=get_working_page)
    browser_state.navigate_to_url = AsyncMock()
    browser_state.set_active_page = AsyncMock()
    xhr_capture = MagicMock(has_in_flight_requests=False)
    xhr_capture.drain = AsyncMock(return_value=False)
    list_calls = 0

    def list_files(_path: Path | str) -> list[str]:
        nonlocal list_calls, page_closed
        list_calls += 1
        if list_calls == 2:
            page_closed = True
        return []

    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE = MagicMock()
    blocked_inline_recovery = AsyncMock(return_value=None)

    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(ActionHandler, "_handle_action", new=AsyncMock(return_value=[ActionSuccess()])),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", return_value=xhr_capture),
            patch(
                "skyvern.webeye.actions.handler._recover_blocked_inline_pdf_download",
                new=blocked_inline_recovery,
            ),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    browser_state.new_page.assert_awaited_once_with()
    browser_state.reconnect.assert_awaited_once_with(
        proxy_location=task.proxy_location,
        workflow_run_id=task.workflow_run_id,
        workflow_permanent_id=task.workflow_permanent_id,
        organization_id=task.organization_id,
        extra_http_headers=task.extra_http_headers,
        cdp_connect_headers=task.cdp_connect_headers,
        browser_address=task.browser_address,
        browser_profile_id=None,
    )
    browser_state.navigate_to_url.assert_awaited_once_with(page=recovered_page, url=page.url)
    browser_state.set_active_page.assert_awaited_once_with(recovered_page)
    blocked_inline_recovery.assert_not_awaited()
    assert results[-1].download_triggered is False
    assert results[-1].skip_remaining_actions is True


@pytest.mark.asyncio
async def test_handle_action_restores_blank_working_page_when_download_not_triggered() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/downloads",
    )
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])
    page.context._skyvern_cdp_download_active = False
    browser_state.navigate_to_url = AsyncMock()
    xhr_capture = MagicMock(has_in_flight_requests=False)
    xhr_capture.drain = AsyncMock(return_value=False)

    async def strand_page_on_blank(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        # The download-intent click navigates the only tab away, leaving it holding no document.
        page.url = "about:blank"
        return [ActionSuccess()]

    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE = MagicMock()

    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=strand_page_on_blank),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", return_value=xhr_capture),
            patch(
                "skyvern.webeye.actions.handler._recover_blocked_inline_pdf_download",
                new=AsyncMock(return_value=None),
            ),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is False
    browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.com/downloads")
    # The restore replaced the document the batch was planned against.
    assert results[-1].skip_remaining_actions is True


@pytest.mark.asyncio
async def test_handle_action_stops_batch_after_restoring_blank_page_when_download_triggered() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/downloads",
    )
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])
    page.context._skyvern_cdp_download_active = False
    browser_state.navigate_to_url = AsyncMock()

    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE = MagicMock()

    with tempfile.TemporaryDirectory() as temp_dir:
        downloaded_file = Path(temp_dir) / "report.pdf"

        async def strand_page_on_blank(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
            # The download lands, but the click still left the tab holding no document.
            page.url = "about:blank"
            downloaded_file.write_bytes(b"downloaded")
            return [ActionSuccess()]

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=strand_page_on_blank),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch(
                "skyvern.webeye.actions.handler.list_files_in_directory",
                side_effect=lambda _path: [str(downloaded_file)] if downloaded_file.exists() else [],
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    browser_state.navigate_to_url.assert_awaited_once_with(page=page, url="https://example.com/downloads")
    # Same as the untriggered path: the restore replaced the document the batch was planned against.
    assert results[-1].skip_remaining_actions is True


@pytest.mark.asyncio
async def test_handle_action_does_not_close_recovered_page_when_download_finishes() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/downloads",
    )
    download_popup = MagicMock()
    download_popup.url = "https://example.com/download-popup"
    download_popup.close = AsyncMock()
    recovered_page = MagicMock()
    recovered_page.url = page.url
    recovered_page.is_closed.return_value = False
    recovered_page.context = page.context
    recovered_page.close = AsyncMock()
    page.context._skyvern_cdp_download_active = False
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])
    recovered_page.expose_binding = AsyncMock()
    recovered_page.evaluate = AsyncMock(return_value=[])
    browser_state.list_valid_pages = AsyncMock(side_effect=[[page], [download_popup, recovered_page]])
    browser_state.new_page = AsyncMock(return_value=recovered_page)
    browser_state.navigate_to_url = AsyncMock()
    browser_state.set_active_page = AsyncMock()
    observer_lifecycle: list[str] = []
    original_observer = MagicMock()
    original_observer.events = [{"text": "The report download is ready", "timestamp_ms": 1}]
    original_observer.start = AsyncMock(side_effect=lambda **_: observer_lifecycle.append("original:start"))

    async def stop_original_observer() -> None:
        observer_lifecycle.append("original:stop")
        original_observer.events.clear()

    original_observer.stop = AsyncMock(side_effect=stop_original_observer)
    recovered_observer = MagicMock()
    recovered_observer.events = []
    recovered_observer.start = AsyncMock(side_effect=lambda **_: observer_lifecycle.append("recovered:start"))
    recovered_observer.stop = AsyncMock(side_effect=lambda: observer_lifecycle.append("recovered:stop"))

    with tempfile.TemporaryDirectory() as temp_dir:
        downloaded_file = Path(temp_dir) / "report.pdf"

        async def close_page_during_click(*args: object, **kwargs: object) -> list[ActionSuccess]:
            page.is_closed.return_value = True
            downloaded_file.write_bytes(b"downloaded")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=close_page_during_click),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch(
                "skyvern.webeye.actions.handler.list_files_in_directory",
                side_effect=lambda _path: [str(downloaded_file)] if downloaded_file.exists() else [],
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch(
                "skyvern.webeye.actions.handler.TransientPageTextObserver",
                side_effect=[original_observer, recovered_observer],
            ),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    assert results[-1].skip_remaining_actions is True
    assert recovered_observer.events == [{"text": "The report download is ready", "timestamp_ms": 1}]
    assert observer_lifecycle == ["original:start", "original:stop", "recovered:start", "recovered:stop"]
    recovered_page.close.assert_not_awaited()
    download_popup.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_handle_action_closes_failed_recovery_page_and_bounds_navigation() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/downloads",
    )
    task.download_timeout = 0.01
    failed_recovery_page = MagicMock()
    failed_recovery_page.url = "about:blank"
    failed_recovery_page.context = page.context
    failed_recovery_page.close = AsyncMock()
    page.context._skyvern_cdp_download_active = False
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])
    browser_state.new_page = AsyncMock(return_value=failed_recovery_page)
    navigation_started = asyncio.Event()

    async def hang_during_recovery(*args: object, **kwargs: object) -> None:
        navigation_started.set()
        await asyncio.Event().wait()

    browser_state.navigate_to_url = AsyncMock(side_effect=hang_during_recovery)
    browser_state.set_active_page = AsyncMock()

    async def close_page_during_click(*args: object, **kwargs: object) -> list[ActionSuccess]:
        page.is_closed.return_value = True
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=close_page_during_click),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.LOG.warning"),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )

    assert navigation_started.is_set()
    browser_state.navigate_to_url.assert_awaited_once_with(
        page=failed_recovery_page,
        url="https://example.com/downloads",
    )
    browser_state.set_active_page.assert_not_awaited()
    failed_recovery_page.close.assert_awaited_once_with()
    assert results[-1].download_triggered is False
    assert results[-1].skip_remaining_actions is not True


@pytest.mark.asyncio
async def test_handle_action_timeout_bounds_browser_download_handler_drain(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task.download_timeout = 0.01
    page.on.side_effect = lambda *args: None
    inert_monitor = MagicMock(name="inert_network_egress_monitor")
    inert_monitor.authorize_request.return_value = False
    inert_authorizer = AsyncMock(
        name="inert_redirect_hop_authorizer",
        side_effect=AssertionError("direct HTTP download is outside this drain test"),
    )
    interceptor = CDPDownloadInterceptor(
        network_egress_monitor=inert_monitor,
        redirect_hop_authorizer=inert_authorizer,
    )
    interceptor._accepting_browser_downloads = True
    page.context._skyvern_cdp_download_interceptor = interceptor
    handler_started = asyncio.Event()
    never_release = asyncio.Event()

    async def hanging_handler(event: dict[str, object]) -> None:
        handler_started.set()
        await never_release.wait()

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging_dir = os.path.join(temp_root, "staging")
        os.makedirs(staging_dir)

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            interceptor._schedule_browser_download_handler({"url": "https://example.com/report.pdf"})
            await handler_started.wait()
            with open(os.path.join(primary_dir, "report.pdf"), "wb") as file:
                file.write(b"ready")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        started_at = time.monotonic()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch.object(interceptor, "_handle_browser_download", side_effect=hanging_handler),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=staging_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    ActionHandler.handle_action(
                        scraped_page=scraped_page,
                        task=task,
                        step=step,
                        page=page,
                        action=action,
                    ),
                    timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
                )

        assert time.monotonic() - started_at < CI_TEST_RUNAWAY_TIMEOUT_SECONDS
        await _assert_background_tasks_drained(interceptor._browser_download_tasks)


@pytest.mark.asyncio
async def test_handle_action_download_completion_may_exceed_signal_budget(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task.download_timeout = None
    clock = _FakeMonotonic()
    clock.advance_after_next_read(0.05)

    with tempfile.TemporaryDirectory() as temp_dir:

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            with open(os.path.join(temp_dir, "report.pdf"), "wb") as file:
                file.write(b"ready")
            return [ActionSuccess()]

        during_completion_wait: list[datetime] = []

        async def slow_download_completion(**kwargs: object) -> None:
            assert kwargs["timeout"] == 0.2
            during_completion_wait.append(datetime.now(UTC).replace(tzinfo=None))

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_MAX_WAIT_TIME", 0.02),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_TIMEOUT", 0.2),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.get_run_temp_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(side_effect=slow_download_completion),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.time", clock),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]
    # The download-triggered persist path stamps the execution window too, and the
    # window excludes the download-completion wait: finished_at was taken when the
    # inner action returned, strictly before the completion mock ran.
    assert action.started_at is not None
    assert action.finished_at is not None
    assert action.started_at <= action.finished_at
    assert during_completion_wait
    assert action.finished_at <= during_completion_wait[0]
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_elapsed_seconds"] == 0.05


@pytest.mark.asyncio
async def test_handle_action_crdownload_signal_enters_completion_before_reporting_final_artifact() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task.download_timeout = 0.05

    with tempfile.TemporaryDirectory() as temp_dir:
        partial_path = Path(temp_dir) / "report.pdf.crdownload"
        final_path = Path(temp_dir) / "report.pdf"

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            partial_path.write_bytes(b"in progress")
            return [ActionSuccess()]

        async def complete_download(**kwargs: object) -> None:
            assert kwargs["timeout"] == task.download_timeout
            partial_path.rename(final_path)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        settle = AsyncMock(side_effect=complete_download)

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=settle,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )

    settle.assert_awaited_once()
    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]
    assert "report.pdf.crdownload" not in results[-1].downloaded_files


@pytest.mark.asyncio
async def test_handle_action_aborted_download_is_reported_as_failure_not_success() -> None:
    # The browser deletes the partial file when it aborts a transfer, so the settle sees the same
    # empty directory a completed download leaves behind. Reporting success here tells the agent the
    # file arrived, and it retries the already-consumed link instead of regenerating it.
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task.download_timeout = 0.05

    with tempfile.TemporaryDirectory() as temp_dir:
        partial_path = Path(temp_dir) / "bundle.zip.crdownload"
        aborted_download = _download(path=partial_path, failure="canceled")
        aborted_download.url = "https://example.com/bundle.zip"

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            partial_path.write_bytes(b"in progress")
            capture = next(call_.args[1] for call_ in page.on.call_args_list if call_.args[0] == "download")
            capture(aborted_download)
            return [ActionSuccess()]

        async def abort_download(**kwargs: object) -> None:
            partial_path.unlink()

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(side_effect=abort_download),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )

    assert results[-1].success is False
    assert results[-1].download_triggered is True
    assert not results[-1].downloaded_files
    assert "canceled" in (results[-1].exception_message or "")


@pytest.mark.asyncio
async def test_handle_action_stale_abort_skips_explicit_download_observation() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    stale_abort = ActionAbort()
    stale_abort.skip_remaining_actions = True
    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)

    with (
        patch.object(ActionHandler, "_handle_action", new=AsyncMock(return_value=[stale_abort])),
        patch("skyvern.webeye.actions.handler.get_download_dir", return_value=tempfile.gettempdir()),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
        patch(
            "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
            new=AsyncMock(side_effect=AssertionError("stale abort entered download observation")),
        ),
        patch("skyvern.webeye.actions.handler.app", mock_app),
    ):
        results = await ActionHandler.handle_action(
            scraped_page=scraped_page,
            task=task,
            step=step,
            page=page,
            action=action,
        )

    assert results == [stale_abort]
    assert results[-1].skip_remaining_actions is True


@pytest.mark.asyncio
async def test_handle_action_remote_crdownload_signal_enters_completion_before_reporting_final_artifact() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task.browser_session_id = "bs-1"
    task.download_timeout = 0.05
    existing_partial_uri = "s3://bucket/browser_sessions/bs-1/downloads/existing.pdf.crdownload"
    new_partial_uri = "s3://bucket/browser_sessions/bs-1/downloads/report.pdf.crdownload"
    final_uri = "s3://bucket/browser_sessions/bs-1/downloads/report.pdf"
    downloading_uris = [existing_partial_uri]
    downloaded_uris: list[str] = []

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        downloading_uris.append(new_partial_uri)
        return [ActionSuccess()]

    async def complete_download(**kwargs: object) -> None:
        assert kwargs["timeout"] == task.download_timeout
        downloading_uris.remove(new_partial_uri)
        downloaded_uris.append(final_uri)

    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(
        side_effect=lambda **_: downloaded_uris.copy()
    )
    mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(
        side_effect=lambda **_: downloading_uris.copy()
    )
    settle = AsyncMock(side_effect=complete_download)

    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=settle,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )

    settle.assert_awaited_once()
    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]
    assert all(not filename.endswith(".crdownload") for filename in results[-1].downloaded_files)


@pytest.mark.asyncio
async def test_handle_action_preexisting_remote_crdownload_does_not_signal_new_download() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task.browser_session_id = "bs-1"
    task.download_timeout = 0.01
    existing_partial_uri = "s3://bucket/browser_sessions/bs-1/downloads/existing.pdf.crdownload"
    existing_final_uri = "s3://bucket/browser_sessions/bs-1/downloads/existing.pdf"
    downloading_uris = [existing_partial_uri]
    downloaded_uris: list[str] = []

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        downloading_uris.remove(existing_partial_uri)
        downloaded_uris.append(existing_final_uri)
        return [ActionSuccess()]

    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(
        side_effect=lambda **_: downloaded_uris.copy()
    )
    mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(
        side_effect=lambda **_: downloading_uris.copy()
    )
    settle = AsyncMock()

    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=settle,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )

    settle.assert_not_awaited()
    assert results[-1].download_triggered is False
    assert results[-1].downloaded_files is None


@pytest.mark.asyncio
async def test_handle_action_remote_snapshot_captures_partial_transition_before_completed_files() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task.browser_session_id = "bs-1"
    task.download_timeout = 0.01
    existing_partial_uri = "s3://bucket/browser_sessions/bs-1/downloads/existing.pdf.crdownload"
    existing_final_uri = "s3://bucket/browser_sessions/bs-1/downloads/existing.pdf"
    downloading_uris = [existing_partial_uri]
    downloaded_uris: list[str] = []
    transition_pending = True

    async def list_downloaded_files(**_: object) -> list[str]:
        nonlocal transition_pending
        snapshot = downloaded_uris.copy()
        if transition_pending:
            transition_pending = False
            downloading_uris.remove(existing_partial_uri)
            downloaded_uris.append(existing_final_uri)
        return snapshot

    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(side_effect=list_downloaded_files)
    mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(
        side_effect=lambda **_: downloading_uris.copy()
    )
    settle = AsyncMock()

    with tempfile.TemporaryDirectory() as temp_dir:
        with (
            patch.object(ActionHandler, "_handle_action", new=AsyncMock(return_value=[ActionSuccess()])),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=settle,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )

    settle.assert_not_awaited()
    assert results[-1].download_triggered is False
    assert results[-1].downloaded_files is None


@pytest.mark.asyncio
async def test_handle_action_download_completion_budget_bounds_hanging_settle(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task.download_timeout = None

    with tempfile.TemporaryDirectory() as temp_dir:

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            with open(os.path.join(temp_dir, "report.pdf"), "wb") as file:
                file.write(b"ready")
            return [ActionSuccess()]

        async def hanging_download_completion(**kwargs: object) -> None:
            assert kwargs["timeout"] == 0.03
            await asyncio.Event().wait()

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        started_at = time.monotonic()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_MAX_WAIT_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_TIMEOUT", 0.03),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(side_effect=hanging_download_completion),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    ActionHandler.handle_action(
                        scraped_page=scraped_page,
                        task=task,
                        step=step,
                        page=page,
                        action=action,
                    ),
                    timeout=5,
                )

        elapsed = time.monotonic() - started_at

    # Proves the 0.03s download budget raised, not the 5s wait_for safety net.
    assert elapsed < CI_TEST_RUNAWAY_TIMEOUT_SECONDS


def test_remove_download_listener_uses_playwright_remove_listener_when_off_unavailable() -> None:
    page = MagicMock(spec=["remove_listener"])
    callback = MagicMock()

    _remove_download_listener(page, callback)

    page.remove_listener.assert_called_once_with("download", callback)


def test_remove_download_listener_logs_when_page_lacks_cleanup_api() -> None:
    page = MagicMock(spec=[])
    callback = MagicMock()

    with patch("skyvern.webeye.actions.handler.LOG.warning") as warning:
        _remove_download_listener(page, callback)

    warning.assert_called_once_with("Page does not support removing download listeners")


@pytest.mark.asyncio
async def test_handle_download_file_action_with_byte_data() -> None:
    """Test that when byte data is provided, the file should be saved directly"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    # Create test byte data
    test_bytes = b"test file content"
    action = DownloadFileAction(
        file_name="test_file.txt",
        byte=test_bytes,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    # Mock initialize_download_dir to return a temporary directory
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=temp_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Verify result (download_triggered is set by outer handle action flow when in context)
            assert len(result) == 1
            assert isinstance(result[0], ActionSuccess)

            # Verify file was created
            expected_file_path = os.path.join(temp_dir, "test_file.txt")
            assert os.path.exists(expected_file_path)

            # Verify file content
            with open(expected_file_path, "rb") as f:
                assert f.read() == test_bytes


def _download_redirect_chain(*urls: str) -> SimpleNamespace:
    """page.goto-style response whose followed redirect chain visited ``urls`` in order."""
    request: SimpleNamespace | None = None
    for url in urls:
        request = SimpleNamespace(url=url, redirected_from=request)
    return SimpleNamespace(request=request)


def _refuse_metadata_hop(url: str) -> str:
    if "169.254.169.254" in url:
        raise BlockedHost(host=url)
    return url


@pytest.mark.asyncio
async def test_handle_download_file_action_refuses_a_blocked_redirect_hop() -> None:
    """A public download_url that redirects to the metadata endpoint must fail closed."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.goto = AsyncMock(
        return_value=_download_redirect_chain(
            "https://example.com/file.pdf", "http://169.254.169.254/latest/meta-data/"
        )
    )
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="downloaded_file.pdf",
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with (
        patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"),
        patch("skyvern.webeye.actions.handler.validate_fetch_url", side_effect=_refuse_metadata_hop),
    ):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

    # This handler wraps its whole body in ``except Exception -> ActionFailure``, so a blocked hop
    # surfaces as a failed action rather than propagating. The pre-existing entry-URL check on this
    # path already behaves the same way; what matters is that it never reports success.
    assert len(result) == 1
    assert isinstance(result[0], ActionFailure)
    assert result[0].exception_type == BlockedHost.__name__
    assert page.goto.await_args_list == [
        call("https://example.com/file.pdf", timeout=settings.BROWSER_LOADING_TIMEOUT_MS),
        call("about:blank"),
    ]


@pytest.mark.asyncio
async def test_handle_download_file_action_with_download_url() -> None:
    """Test that when download_url is provided, page.goto is called and returns ActionSuccess"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="downloaded_file.pdf",
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with (
        patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"),
        patch("skyvern.webeye.actions.handler.validate_fetch_url", side_effect=lambda url: url),
    ):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

        # Verify page.goto was called with the correct URL (handler uses browser navigation for download_url)
        page.goto.assert_called_once()
        assert page.goto.call_args[0][0] == "https://example.com/file.pdf"

        # Verify result
        assert len(result) == 1
        assert isinstance(result[0], ActionSuccess)


@pytest.mark.asyncio
async def test_handle_download_file_action_with_download_url_same_filename() -> None:
    """Test that when download_url is provided, page.goto is called with the URL and returns ActionSuccess"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="same_name.pdf",
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with (
        patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"),
        patch("skyvern.webeye.actions.handler.validate_fetch_url", side_effect=lambda url: url),
    ):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

        page.goto.assert_called_once()
        assert page.goto.call_args[0][0] == "https://example.com/file.pdf"

        assert len(result) == 1
        assert isinstance(result[0], ActionSuccess)


@pytest.mark.asyncio
async def test_handle_download_file_action_without_byte_or_url() -> None:
    """Test that when neither byte data nor download_url is provided, should return ActionSuccess (no download triggered)."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="test_file.txt",
        byte=None,
        download_url=None,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=temp_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Verify result (download_triggered is set by outer handle action flow when in context)
            assert len(result) == 1
            assert isinstance(result[0], ActionSuccess)


@pytest.mark.asyncio
async def test_handle_download_file_action_with_byte_priority() -> None:
    """Test that when both byte and download_url are provided, byte data should take priority"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    # Create test byte data
    test_bytes = b"byte data content"
    action = DownloadFileAction(
        file_name="test_file.txt",
        byte=test_bytes,
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    page.goto = AsyncMock(return_value=None)

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=temp_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Byte data takes priority: page.goto should not be called
            page.goto.assert_not_called()

            assert len(result) == 1
            assert isinstance(result[0], ActionSuccess)

            expected_file_path = os.path.join(temp_dir, "test_file.txt")
            assert os.path.exists(expected_file_path)
            with open(expected_file_path, "rb") as f:
                assert f.read() == test_bytes


@pytest.mark.asyncio
async def test_handle_download_file_action_with_file_name_empty() -> None:
    """Test that when file_name is empty string, UUID should be used as filename"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    test_bytes = b"test content"
    action = DownloadFileAction(
        file_name="",  # Empty string, handler will use UUID
        byte=test_bytes,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=temp_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Verify result (download_triggered is set by outer handle action flow when in context)
            assert len(result) == 1
            assert isinstance(result[0], ActionSuccess)

            # Verify file was created (filename should be UUID)
            files = os.listdir(temp_dir)
            assert len(files) == 1
            # Verify file content
            file_path = os.path.join(temp_dir, files[0])
            with open(file_path, "rb") as f:
                assert f.read() == test_bytes


@pytest.mark.asyncio
async def test_handle_download_file_action_download_url_error() -> None:
    """Test that when download_url download fails, should return ActionFailure"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="test_file.txt",
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    page.goto = AsyncMock(side_effect=Exception("Download failed"))

    with (
        patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"),
        patch("skyvern.webeye.actions.handler.validate_fetch_url", side_effect=lambda url: url),
    ):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

        assert len(result) == 1
        assert isinstance(result[0], ActionFailure)
        assert result[0].exception_type == "Exception"
        assert result[0].exception_message == "Download failed"


@pytest.mark.asyncio
async def test_handle_download_file_action_file_write_error() -> None:
    """Test that when file write fails, should return ActionFailure"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    test_bytes = b"test content"
    action = DownloadFileAction(
        file_name="test_file.txt",
        byte=test_bytes,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    # Mock initialize_download_dir to return an invalid path (e.g., read-only directory)
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a read-only directory to simulate write failure
        read_only_dir = os.path.join(temp_dir, "readonly")
        os.makedirs(read_only_dir, mode=0o555)

        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=read_only_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Verify result should be ActionFailure
            assert len(result) == 1
            assert isinstance(result[0], ActionFailure)


@pytest.mark.asyncio
async def test_handle_download_file_action_download_url_err_aborted_swallowed() -> None:
    """Test that when page.goto raises net::ERR_ABORTED (browser download flow), error is swallowed and returns ActionSuccess"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.goto = AsyncMock(side_effect=Exception("net::ERR_ABORTED at https://example.com/file.pdf"))
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="test_file.txt",
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with (
        patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"),
        patch("skyvern.webeye.actions.handler.validate_fetch_url", side_effect=lambda url: url),
    ):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

        assert len(result) == 1
        assert isinstance(result[0], ActionSuccess)


@pytest.mark.asyncio
async def test_handle_action_navigates_back_from_blank_page_after_download(
    span_exporter: InMemorySpanExporter,
) -> None:
    """After a print/download click the working page sometimes navigates to about:blank.
    handle_action should detect this and navigate back to the original URL so the
    next step is not stuck on a blank page."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    original_url = "https://example.com/document/123"

    # Page starts at a real URL; the mocked action will navigate it to about:blank
    page = MagicMock()
    page.url = original_url

    browser_state = MagicMock()
    # No cloud factory attached a vendor download source here; keep the provider seam inert.
    browser_state.browser_artifacts.get_action_download_source.return_value = None
    # Same page count before and after (no extra tab opened by the print action)
    browser_state.list_valid_pages = AsyncMock(return_value=[page])
    browser_state.navigate_to_url = AsyncMock()

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="btn-print",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )
    clock = _FakeMonotonic()
    clock.advance_after_next_read(1.2)

    # _handle_action simulates the page navigating to about:blank during the print download
    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        page.url = "about:blank"
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        dummy_file = os.path.join(temp_dir, "doc.pdf")
        with open(dummy_file, "w") as f:
            f.write("dummy")

        # list_files_in_directory: empty before action, one file after action, re-scan after wait
        list_files_side_effect = [[], [dummy_file], [dummy_file]]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files_side_effect),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.time", clock),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    # The blank-page recovery should have navigated back to the original URL
    browser_state.navigate_to_url.assert_called_once_with(page=page, url=original_url)
    # A successful download must not attach the no-download followup feedback.
    assert results[-1].download_triggered is True
    assert results[-1].needs_followup is None
    assert results[-1].followup_message is None
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "download_file_detected"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert span_attrs["download_signal_elapsed_seconds"] == 1.2


@pytest.mark.asyncio
async def test_handle_action_does_not_navigate_back_when_page_url_unchanged() -> None:
    """When the page URL does not change to blank after a download, navigate_to_url should NOT be called."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    original_url = "https://example.com/document/123"

    page = MagicMock()
    page.url = original_url  # URL stays the same after download

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])
    browser_state.navigate_to_url = AsyncMock()

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="btn-print",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    # _handle_action does NOT change the page URL (normal case)
    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        dummy_file = os.path.join(temp_dir, "doc.pdf")
        with open(dummy_file, "w") as f:
            f.write("dummy")

        list_files_side_effect = [[], [dummy_file], [dummy_file]]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files_side_effect),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    # Page URL is unchanged; no navigation back should occur
    browser_state.navigate_to_url.assert_not_called()


@pytest.mark.asyncio
async def test_handle_action_download_no_signal_fails_fast(span_exporter: InMemorySpanExporter) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
        download_timeout=30.0,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/no-download"
    download_listeners: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_listeners.__setitem__(event, callback)
    page.context.browser = None

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        started_at = time.monotonic()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_EXTENSION_MAX_SECONDS", 0.1),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0.01),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )
        elapsed = time.monotonic() - started_at

    assert elapsed < CI_TEST_RUNAWAY_TIMEOUT_SECONDS
    assert results[-1].download_triggered is False
    assert action.download_triggered is False
    assert results[-1].needs_followup is True
    assert results[-1].followup_message == DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE
    assert wait_for_downloads.await_count == 0
    page.off.assert_any_call("download", download_listeners["download"])
    # Managed (non-adopted) sessions now gain the identity-only download-popup claim recorder, but
    # must not gain popup-download-EVENT wiring: firing the recorded popup listener attaches no
    # download listener to the popup (SKY-12621 invariant preserved).
    popup_cb = download_listeners.get("popup")
    assert popup_cb is not None
    sentinel_popup = MagicMock()
    popup_cb(sentinel_popup)
    sentinel_popup.on.assert_not_called()
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is False
    assert span_attrs["download_wait_extended_for_in_flight_request"] is False
    assert "download_signal_source" not in span_attrs
    assert "download_signal_elapsed_seconds" not in span_attrs
    assert "download_signal_poll_iterations" not in span_attrs


@pytest.mark.asyncio
async def test_handle_action_download_no_signal_preserves_action_failure() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/no-download",
    )
    action.errors = []
    failure = ActionFailure(RuntimeError("click failed"))

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionFailure]:
        return [failure]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results == [failure]
    assert results[-1] is failure
    assert isinstance(results[-1], ActionFailure)
    assert results[-1].success is False
    assert results[-1].download_triggered is False
    assert action.download_triggered is False
    assert action.errors == []
    assert results[-1].needs_followup is None
    assert results[-1].followup_message is None


@pytest.mark.asyncio
async def test_handle_action_download_fails_on_transient_user_defined_error_text(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/portal/invoices",
        task_overrides={
            "error_code_mapping": {
                "data_not_downloadable": (
                    "Return this error if the page displays "
                    "download failure says the generated archive could not be saved"
                ),
            },
        },
    )
    existing_error = UserDefinedError(
        error_code="previous_error",
        reasoning="Earlier action error",
        confidence_float=0.8,
    )
    action.errors = [existing_error]
    page.evaluate = AsyncMock()
    download_listeners: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_listeners.__setitem__(event, callback)

    async def expose_binding(_name: str, callback: Callable[[dict, dict], None]) -> None:
        page._transient_text_callback = callback

    page.expose_binding = AsyncMock(side_effect=expose_binding)
    mock_xhr = MagicMock()
    mock_xhr.has_in_flight_requests = True
    mock_xhr.drain = AsyncMock(return_value=False)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        page._transient_text_callback(
            {},
            {
                "text": "Example download failure says the generated archive could not be saved",
                "timestamp_ms": 1,
                "tag": "DIV",
                "role": "alert",
            },
        )
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        staging_dir = Path(temp_dir) / "staging"
        staging_dir.mkdir()
        (staging_dir / "completed.pdf").write_bytes(b"%PDF-1.4 completed")
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        started_at = time.monotonic()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", return_value=mock_xhr),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )
        elapsed = time.monotonic() - started_at

    assert elapsed < CI_TEST_RUNAWAY_TIMEOUT_SECONDS
    assert isinstance(results[-1], ActionFailure)
    assert results[-1].download_triggered is False
    assert "download failure says the generated archive could not be saved" in (results[-1].exception_message or "")
    assert action.download_triggered is False
    # Page-confirmed terminal user errors are definitive: no "keep trying" followup.
    assert results[-1].needs_followup is None
    assert results[-1].followup_message is None
    assert action.errors is not None
    assert [error.error_code for error in action.errors] == ["previous_error", "data_not_downloadable"]
    assert action.terminal_user_errors is True
    assert wait_for_downloads.await_count == 0
    page.off.assert_any_call("download", download_listeners["download"])
    assert page.expose_binding.await_count == 1
    observer_install_count = sum(
        "new MutationObserver" in call.kwargs["expression"] for call in page.evaluate.await_args_list
    )
    assert observer_install_count == 2
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is False
    assert span_attrs["download_wait_observed_text_count"] == 1
    assert span_attrs["download_wait_user_error_detected"] is True
    assert span_attrs["download_wait_user_error_codes"] == "data_not_downloadable"
    assert mock_xhr.drain.await_args_list == [call(timeout_seconds=0), call(timeout_seconds=0)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post_action_text, expect_terminal_error", [(None, False), ("The archive could not be saved", True)]
)
async def test_handle_action_download_scans_semantic_alerts_only_after_action(
    span_exporter: InMemorySpanExporter,
    post_action_text: str | None,
    expect_terminal_error: bool,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
        task_overrides={"error_code_mapping": {"archive_failed": "archive could not be saved"}},
    )
    callbacks: dict[str, Callable] = {}
    visible_alert_text = "A stale archive could not be saved"
    baseline_alert_text: str | None = None

    async def expose_binding(_name: str, callback: Callable[[dict, dict], None]) -> None:
        callbacks["transient_text"] = callback

    async def evaluate(*, expression: str, arg: dict) -> None:
        nonlocal baseline_alert_text
        assert "new MutationObserver" in expression or "delete window[stateKey]" in expression
        if not arg.get("scanInitialVisibleState"):
            baseline_alert_text = visible_alert_text
        elif visible_alert_text and visible_alert_text != baseline_alert_text:
            callbacks["transient_text"]({}, {"text": visible_alert_text, "role": "alert"})

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        nonlocal visible_alert_text
        if post_action_text is not None:
            visible_alert_text = post_action_text
        return [ActionSuccess()]

    page.expose_binding = AsyncMock(side_effect=expose_binding)
    page.evaluate = AsyncMock(side_effect=evaluate)

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    install_options = [
        call.kwargs["arg"]
        for call in page.evaluate.await_args_list
        if "new MutationObserver" in call.kwargs["expression"]
    ]
    assert [options["scanInitialVisibleState"] for options in install_options] == [False, True]
    assert isinstance(results[-1], ActionFailure) is expect_terminal_error
    assert action.terminal_user_errors is expect_terminal_error
    assert [error.error_code for error in action.errors or []] == (["archive_failed"] if expect_terminal_error else [])


@pytest.mark.asyncio
async def test_handle_action_download_admits_request_event_queued_by_action(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
        task_overrides={
            "error_code_mapping": {
                "download_failed": "generated document could not be prepared for download",
            },
        },
    )
    task = task.model_copy(update={"download_timeout": None})
    callbacks: dict[str, Callable] = {}
    page.context._skyvern_cdp_download_active = False
    page.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)

    async def expose_binding(_name: str, callback: Callable[[dict, dict], None]) -> None:
        callbacks["transient_text"] = callback

    page.expose_binding = AsyncMock(side_effect=expose_binding)
    page.evaluate = AsyncMock()
    request = MagicMock(resource_type="xhr")
    late_tasks: list[asyncio.Task[None]] = []
    first_download_wait_poll = asyncio.Event()
    list_calls = 0
    clock = _FakeMonotonic()
    clock.advance_after_next_read(0.01)

    def list_files(_download_dir: object) -> list[str]:
        nonlocal list_calls
        list_calls += 1
        if list_calls == 2:
            first_download_wait_poll.set()
        return []

    async def expose_error_after_grace() -> None:
        await first_download_wait_poll.wait()
        callbacks["transient_text"](
            {},
            {"text": "The generated document could not be prepared for download", "timestamp_ms": 1},
        )
        callbacks["requestfinished"](request)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        asyncio.get_running_loop().call_soon(callbacks["request"], request)
        late_tasks.append(asyncio.create_task(expose_error_after_grace()))
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.time", clock),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )
        await asyncio.gather(*late_tasks)

    assert isinstance(results[-1], ActionFailure)
    assert [error.error_code for error in action.errors or []] == ["download_failed"]
    assert clock.current == 0.01
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_wait_extended_for_in_flight_request"] is True
    for event in ("response", "request", "requestfinished", "requestfailed"):
        page.remove_listener.assert_any_call(event, callbacks[event])


@pytest.mark.asyncio
async def test_handle_action_download_observes_error_after_grace_while_xhr_is_in_flight(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
        task_overrides={
            "error_code_mapping": {
                "download_failed": "generated document could not be prepared for download",
            },
        },
    )
    task = task.model_copy(update={"download_timeout": None})
    callbacks: dict[str, Callable] = {}
    page.context._skyvern_cdp_download_active = False
    page.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)

    async def expose_binding(_name: str, callback: Callable[[dict, dict], None]) -> None:
        callbacks["transient_text"] = callback

    page.expose_binding = AsyncMock(side_effect=expose_binding)
    page.evaluate = AsyncMock()
    request = MagicMock(resource_type="xhr")
    late_event_finished = asyncio.Event()
    first_download_wait_poll = asyncio.Event()
    list_calls = 0
    clock = _FakeMonotonic()
    clock.advance_after_next_read(0.01)

    def list_files(_download_dir: object) -> list[str]:
        nonlocal list_calls
        list_calls += 1
        if list_calls == 2:
            first_download_wait_poll.set()
        return []

    async def finish_request_after_grace() -> None:
        await first_download_wait_poll.wait()
        callbacks["transient_text"](
            {},
            {"text": "The generated document could not be prepared for download", "timestamp_ms": 1},
        )
        callbacks["requestfinished"](request)
        late_event_finished.set()

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        callbacks["request"](request)
        asyncio.create_task(finish_request_after_grace())
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.time", clock),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert late_event_finished.is_set()
    assert isinstance(results[-1], ActionFailure)
    assert [error.error_code for error in action.errors or []] == ["download_failed"]
    assert action.terminal_user_errors is True
    assert clock.current == 0.01
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_wait_extended_for_in_flight_request"] is True
    assert span_attrs["download_wait_user_error_detected"] is True
    for event in ("response", "request", "requestfinished", "requestfailed"):
        page.remove_listener.assert_any_call(event, callbacks[event])


@pytest.mark.asyncio
async def test_handle_action_download_custom_timeout_observes_error_after_grace_while_xhr_is_in_flight(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
        task_overrides={
            "error_code_mapping": {
                "download_failed": "generated document could not be prepared for download",
            },
        },
    )
    task = task.model_copy(update={"download_timeout": 0.1})
    callbacks: dict[str, Callable] = {}
    page.context._skyvern_cdp_download_active = False
    page.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)

    async def expose_binding(_name: str, callback: Callable[[dict, dict], None]) -> None:
        callbacks["transient_text"] = callback

    page.expose_binding = AsyncMock(side_effect=expose_binding)
    page.evaluate = AsyncMock()
    request = MagicMock(resource_type="xhr")
    first_download_wait_poll = asyncio.Event()
    list_calls = 0
    clock = _FakeMonotonic()
    clock.advance_after_next_read(0.01)

    def list_files(_download_dir: object) -> list[str]:
        nonlocal list_calls
        list_calls += 1
        if list_calls == 2:
            first_download_wait_poll.set()
        return []

    async def expose_error_after_grace() -> None:
        await first_download_wait_poll.wait()
        callbacks["transient_text"](
            {},
            {"text": "The generated document could not be prepared for download", "timestamp_ms": 1},
        )
        callbacks["requestfinished"](request)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        callbacks["request"](request)
        asyncio.create_task(expose_error_after_grace())
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.time", clock),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert isinstance(results[-1], ActionFailure)
    assert [error.error_code for error in action.errors or []] == ["download_failed"]
    assert action.terminal_user_errors is True
    assert clock.current == 0.01
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["no_signal_grace_seconds"] == 0.01
    assert span_attrs["timeout_seconds"] == 0.1
    assert span_attrs["download_wait_extended_for_in_flight_request"] is True
    assert span_attrs["download_wait_user_error_detected"] is True


@pytest.mark.asyncio
async def test_handle_action_download_in_flight_request_does_not_extend_custom_timeout(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task = task.model_copy(update={"download_timeout": 0.01})
    callbacks: dict[str, Callable] = {}
    page.context._skyvern_cdp_download_active = False
    page.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock()
    request = MagicMock(resource_type="fetch")

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        callbacks["request"](request)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        started_at = time.monotonic()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_EXTENSION_MAX_SECONDS", 0.5),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0.005),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )
        elapsed = time.monotonic() - started_at

    assert elapsed < CI_TEST_RUNAWAY_TIMEOUT_SECONDS
    assert results[-1].download_triggered is False
    assert action.download_triggered is False
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["no_signal_grace_seconds"] == 0.01
    assert span_attrs["timeout_seconds"] == 0.01
    assert span_attrs["download_wait_extended_for_in_flight_request"] is False


@pytest.mark.asyncio
async def test_handle_action_download_without_explicit_timeout_has_bounded_in_flight_extension(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task = task.model_copy(update={"download_timeout": None})
    callbacks: dict[str, Callable] = {}
    page.context._skyvern_cdp_download_active = False
    page.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock()
    request = MagicMock(resource_type="fetch")

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        callbacks["request"](request)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        started_at = time.monotonic()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_EXTENSION_MAX_SECONDS", 0.03),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0.005),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )
        elapsed = time.monotonic() - started_at

    assert 0.03 <= elapsed < CI_TEST_RUNAWAY_TIMEOUT_SECONDS
    assert results[-1].download_triggered is False
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["no_signal_grace_seconds"] == 0.01
    assert span_attrs["timeout_seconds"] == 0.04
    assert span_attrs["download_wait_extended_for_in_flight_request"] is True


@pytest.mark.asyncio
async def test_handle_action_download_cancellation_cleans_extended_wait_listeners(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task = task.model_copy(update={"download_timeout": None})
    callbacks: dict[str, Callable] = {}
    page.context._skyvern_cdp_download_active = False
    page.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock()
    request = MagicMock(resource_type="fetch", redirected_from=None)
    response = MagicMock(
        request=request,
        status=200,
        url="https://example.com/report.pdf",
        headers={
            "content-type": "application/pdf",
            "content-disposition": 'inline; filename="report.pdf"',
        },
    )
    body_cancelled = asyncio.Event()
    body_started = asyncio.Event()
    clock = _FakeMonotonic()
    clock.advance_after_next_read(0.01)

    async def never_resolving_body() -> bytes:
        body_started.set()
        try:
            await asyncio.Event().wait()
            return b"%PDF-1.4 late"
        finally:
            body_cancelled.set()

    response.body = AsyncMock(side_effect=never_resolving_body)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        callbacks["request"](request)
        callbacks["response"](response)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        staging_dir = Path(temp_dir) / "staging"
        staging_dir.mkdir()
        captures: list[ScopedXhrDownloadCapture] = []

        def make_capture(*args: object, **kwargs: object) -> ScopedXhrDownloadCapture:
            capture = ScopedXhrDownloadCapture(*args, **kwargs)
            captures.append(capture)
            return capture

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_EXTENSION_MAX_SECONDS", 1.0),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", side_effect=make_capture),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.time", clock),
        ):
            handle_task = asyncio.create_task(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )
            )
            await asyncio.wait_for(body_started.wait(), timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS)
            assert not handle_task.done()
            handle_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handle_task

        assert body_cancelled.is_set()
        await _assert_background_tasks_drained(captures[0]._response_tasks)
        assert captures[0]._drained.is_set()
        assert not staging_dir.exists()
        assert not (staging_dir / "report.pdf").exists()
        assert clock.current == 0.01

    for event in ("response", "request", "requestfinished", "requestfailed"):
        page.remove_listener.assert_any_call(event, callbacks[event])
    page.context.remove_listener.assert_called_once_with("page", page.context.on.call_args.args[1])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_wait_extended_for_in_flight_request"] is True


@pytest.mark.asyncio
async def test_handle_action_prefers_observed_file_over_download_event_copy(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When the active run directory receives the file normally, the Playwright
    download event should only act as a signal and should not create a duplicate."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.is_closed.return_value = False
    page.context.browser = None
    settle_active = False
    settle_count = 0

    class _Settle:
        async def __aenter__(self) -> None:
            nonlocal settle_active, settle_count
            settle_active = True
            settle_count += 1

        async def __aexit__(self, *args: object) -> None:
            nonlocal settle_active
            settle_active = False

    interceptor = MagicMock()
    interceptor.settle_browser_downloads.side_effect = _Settle
    page.context._skyvern_cdp_download_interceptor = interceptor
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    # No cloud factory attached a vendor download source here; keep the provider seam inert.
    browser_state.browser_artifacts.get_action_download_source.return_value = None
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.save_as = AsyncMock()
    clock = _FakeMonotonic()
    clock.advance_after_next_read(1.2)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            download_callbacks["download"](download)
            with open(os.path.join(primary_dir, "report.pdf"), "w") as f:
                f.write("dummy")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        async def assert_wait_inside_settle(**kwargs: object) -> None:
            assert settle_active

        wait_for_downloads = AsyncMock(side_effect=assert_wait_inside_settle)

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.time", clock),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]
    assert action.download_triggered is True
    assert action.downloaded_files == results[-1].downloaded_files
    assert wait_for_downloads.await_count == 1
    assert settle_count == 1
    download.save_as.assert_not_awaited()
    page.off.assert_any_call("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert span_attrs["download_signal_elapsed_seconds"] == 1.2


@pytest.mark.asyncio
async def test_handle_action_copies_download_event_when_no_observed_file_appears(
    span_exporter: InMemorySpanExporter,
) -> None:
    """A browser launched before a task/run id may still emit downloads in its
    original directory; after a grace period, copy the event into the active run directory."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    # No cloud factory attached a vendor download source here; keep the provider seam inert.
    browser_state.browser_artifacts.get_action_download_source.return_value = None
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"

    async def save_download(target_path: str | os.PathLike[str]) -> None:
        with open(target_path, "w") as f:
            f.write("dummy")

    download.save_as = AsyncMock(side_effect=save_download)
    clock = _FakeMonotonic()
    clock.advance_after_next_read(1.2)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS", 0),
            patch("skyvern.webeye.actions.handler.time", clock),
            patch(
                "skyvern.webeye.actions.handler._persist_captured_download", wraps=_persist_captured_download
            ) as persist,
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    assert len(results[-1].downloaded_files) == 1
    assert results[-1].downloaded_files[0].endswith("-report.pdf")
    assert action.download_triggered is True
    assert action.downloaded_files == results[-1].downloaded_files
    assert wait_for_downloads.await_count == 1
    download.save_as.assert_awaited_once()
    persist.assert_awaited_once()
    saved_path = download.save_as.await_args.args[0]
    assert os.path.dirname(saved_path) == primary_dir
    page.off.assert_any_call("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert span_attrs["download_signal_elapsed_seconds"] == 1.2


@pytest.mark.asyncio
async def test_handle_action_ignores_empty_download_event_fallback_file(
    span_exporter: InMemorySpanExporter,
) -> None:
    """An empty event fallback artifact should not be reported as a downloaded file."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
        download_timeout=0.01,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    # No cloud factory attached a vendor download source here; keep the provider seam inert.
    browser_state.browser_artifacts.get_action_download_source.return_value = None
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    clock = _FakeMonotonic()
    clock.advance_after_next_read(1.2)

    async def save_empty_download(target_path: str | os.PathLike[str]) -> None:
        open(target_path, "w").close()

    download.save_as = AsyncMock(side_effect=save_empty_download)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS", 0),
            patch("skyvern.webeye.actions.handler.time", clock),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        remaining_files = os.listdir(primary_dir)

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files is None
    assert action.download_triggered is True
    assert action.downloaded_files is None
    assert remaining_files == []
    assert wait_for_downloads.await_count == 1
    download.save_as.assert_awaited_once()
    page.off.assert_any_call("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert span_attrs["download_signal_elapsed_seconds"] == 1.2


@pytest.mark.asyncio
async def test_observed_download_zero_artifacts_reports_needs_followup() -> None:
    # A download event was observed and credited (download_triggered=True), but finalization saved no
    # file and the browser reported no abort reason. Returning a plain success implies a file exists;
    # the action must instead flag needs_followup so the agent keeps trying rather than declaring the
    # goal complete against a file that was never captured.
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
        download_timeout=0.01,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    clock = _FakeMonotonic()
    clock.advance_after_next_read(1.2)

    async def save_empty_download(target_path: str | os.PathLike[str]) -> None:
        open(target_path, "w").close()

    download.save_as = AsyncMock(side_effect=save_empty_download)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS", 0),
            patch("skyvern.webeye.actions.handler.time", clock),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files is None
    assert isinstance(results[-1], ActionSuccess)
    assert results[-1].needs_followup is True
    assert results[-1].followup_message is not None


@pytest.mark.asyncio
async def test_handle_action_stops_after_download_event_fallback_failure(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
        download_timeout=30.0,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.is_closed.return_value = False
    page.context.browser = None
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.save_as = AsyncMock(side_effect=RuntimeError("copy failed"))

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        started_at = time.monotonic()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS", 0),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )
        elapsed = time.monotonic() - started_at

    assert elapsed < CI_TEST_RUNAWAY_TIMEOUT_SECONDS
    assert results[-1].download_triggered is False
    assert action.download_triggered is False
    assert wait_for_downloads.await_count == 0
    download.save_as.assert_awaited_once()
    page.off.assert_any_call("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_event_fallback_attempted"] is True
    assert span_attrs["download_event_fallback_used"] is False
    assert span_attrs["download_event_fallback_failed"] is True


@pytest.mark.asyncio
async def test_handle_action_removes_late_zero_byte_duplicate_after_download_wait() -> None:
    """A 0-byte duplicate that appears after the first download signal should be removed.

    The polling loop exits as soon as one new file appears. Browser-native
    downloads can still surface a second empty duplicate artifact while waiting
    for ``.crdownload`` files to settle; that junk file must not be left for
    task cleanup to upload.
    """
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    download_listeners: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_listeners.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        good_file = os.path.join(primary_dir, "report.pdf")
        empty_file = os.path.join(primary_dir, "report_1.pdf")

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            with open(good_file, "wb") as f:
                f.write(b"valid report")
            return [ActionSuccess()]

        async def wait_then_create_empty_file(*args: object, **kwargs: object) -> None:
            with open(empty_file, "wb"):
                pass

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(side_effect=wait_then_create_empty_file),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        remaining_files = sorted(os.listdir(primary_dir))

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]
    assert action.downloaded_files == ["report.pdf"]
    assert remaining_files == ["report.pdf"]
    page.off.assert_any_call("download", download_listeners["download"])


@pytest.mark.asyncio
async def test_handle_action_removes_download_listener_when_inner_action_raises() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=RuntimeError("boom")),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )

    page.off.assert_any_call("download", download_callbacks["download"])


@pytest.mark.asyncio
async def test_handle_action_discards_xhr_staging_when_native_file_present(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )
    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.save_as = AsyncMock()

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "report.pdf"), "wb") as f:
                f.write(b"xhr content")
            callbacks["download"](download)
            with open(os.path.join(primary_dir, "native-guid.pdf"), "wb") as f:
                f.write(b"native content")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=staging),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        assert results[-1].download_triggered is True
        assert results[-1].downloaded_files == ["native-guid.pdf"]
        assert not os.path.exists(staging)


@pytest.mark.asyncio
async def test_handle_action_uses_xhr_staging_fallback_when_no_native_file(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )
    task.download_timeout = 0.01

    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "report.pdf"), "wb") as f:
                f.write(b"xhr-only content")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            # No native file lands in the observed dir, so the wait loop would burn
            # the full no-signal grace before falling back to xhr staging; shorten it.
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=staging),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        assert results[-1].download_triggered is True
        assert results[-1].downloaded_files == ["report.pdf"]
        assert os.path.isfile(os.path.join(primary_dir, "report.pdf"))
        assert not os.path.exists(staging)


@pytest.mark.asyncio
async def test_handle_action_moves_multiple_staged_xhr_files_as_fallback(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )
    task.download_timeout = 0.01

    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "file_a.pdf"), "wb") as f:
                f.write(b"content a")
            with open(os.path.join(staging, "file_b.zip"), "wb") as f:
                f.write(b"content b")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            # No native file lands in the observed dir, so the wait loop would burn
            # the full no-signal grace before falling back to xhr staging; shorten it.
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=staging),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        assert results[-1].download_triggered is True
        assert sorted(results[-1].downloaded_files) == ["file_a.pdf", "file_b.zip"]


@pytest.mark.asyncio
async def test_handle_action_cleans_staging_on_exception(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )

    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "orphan.pdf"), "wb") as f:
                f.write(b"data")
            raise RuntimeError("simulated crash")

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=staging),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            with pytest.raises(RuntimeError, match="simulated crash"):
                await ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )

        assert not os.path.exists(staging)


@pytest.mark.asyncio
async def test_handle_action_logs_warning_when_late_native_appears_after_xhr_fallback(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When XHR fallback moves staged files and a late native file appears during
    the settle wait, a warning log should be emitted for observability."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )

    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "report.zip"), "wb") as f:
                f.write(b"xhr zip content")
            return [ActionSuccess()]

        async def mock_settle(**kw):
            with open(os.path.join(primary_dir, "native-late.zip"), "wb") as f:
                f.write(b"native zip content different")

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        log_warnings: list[tuple] = []
        original_log = __import__("skyvern.webeye.actions.handler", fromlist=["LOG"]).LOG

        def capture_warning(*args, **kwargs):
            log_warnings.append((args, kwargs))

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=staging),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(side_effect=mock_settle),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch.object(original_log, "warning", side_effect=capture_warning),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        assert results[-1].download_triggered is True
        assert sorted(results[-1].downloaded_files) == ["native-late.zip", "report.zip"]

        race_warnings = [
            (args, kwargs)
            for args, kwargs in log_warnings
            if args and "additional download files appeared" in str(args[0])
        ]
        assert len(race_warnings) == 1
        _, kwargs = race_warnings[0]
        assert kwargs["workflow_run_id"] == "wr-1"
        assert kwargs["xhr_fallback_file_count"] == 1
        assert kwargs["xhr_fallback_files"] == ["report.zip"]
        assert kwargs["post_settle_extra_file_count"] == 1
        assert kwargs["post_settle_extra_files"] == ["native-late.zip"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding_mode", "expected_error"),
    [
        pytest.param(
            "missing-interceptor",
            "requires the page context's CDP download interceptor",
            id="missing-interceptor-fails-closed",
        ),
        pytest.param(
            "missing-authorizer",
            "has no redirect hop authorizer",
            id="missing-authorizer-fails-closed",
        ),
        pytest.param(
            "stale-interceptor",
            "does not own the adopted-session download page context",
            id="stale-context-fails-closed",
        ),
        pytest.param(
            "wrong-download-context",
            "download page context does not match the active page context",
            id="wrong-download-context-fails-closed",
        ),
        pytest.param(
            "missing-download-page",
            "download has no owning page",
            id="missing-download-page-fails-closed",
        ),
    ],
)
async def test_handle_action_adopted_session_requires_context_owned_authorizer(
    binding_mode: str,
    expected_error: str,
) -> None:
    """The adopted-session path follows only the interceptor bound to the download's page context."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id="bs-1",
        download_timeout=30.0,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    page.evaluate = AsyncMock()
    page.expose_binding = AsyncMock()
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.url = "https://example.com/presigned/report.pdf"
    download.page = page
    authorize_request_hop = AsyncMock()
    if binding_mode == "missing-interceptor":
        page.context._skyvern_cdp_download_interceptor_bind_lock = asyncio.Lock()
        delattr(page.context, "_skyvern_cdp_download_interceptor")
    elif binding_mode == "missing-authorizer":
        page.context._skyvern_cdp_download_interceptor_bind_lock = asyncio.Lock()
        page.context._skyvern_cdp_download_interceptor = SimpleNamespace(_page_context=page.context)
    elif binding_mode == "stale-interceptor":
        _bind_adopted_download_authorizer(page, authorize_request_hop, owner_context=object())
    elif binding_mode == "wrong-download-context":
        _bind_adopted_download_authorizer(page, authorize_request_hop)
        download.page = SimpleNamespace(context=object())
    elif binding_mode == "missing-download-page":
        _bind_adopted_download_authorizer(page, authorize_request_hop)
        download.page = None
    page.is_closed.return_value = False

    async def save_download(target_path: str | os.PathLike[str]) -> None:
        with open(target_path, "wb") as f:
            f.write(b"%PDF-1.4 report bytes")

    download.save_as = AsyncMock(side_effect=save_download)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "bs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="bs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            with pytest.raises(RuntimeError, match=expected_error):
                await ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )

        landed_files = sorted(os.listdir(primary_dir))

    assert landed_files == []
    download.save_as.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_action_adopted_session_refetches_when_save_as_target_closed(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When the worker tears the shared browser down and the run's save_as raises while the run
    guarded HTTP client is still available, the adopted-session path re-fetches the replayable URL and the
    bytes still land in the run dir."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id="bs-1",
        download_timeout=30.0,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    page.evaluate = AsyncMock()
    page.expose_binding = AsyncMock()
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)
    authorize_request_hop = AsyncMock()
    _bind_adopted_download_authorizer(page, authorize_request_hop)

    refetched_bytes = b"%PDF-1.4 refetched report bytes"

    async def guarded_fetch(*args: object, **kwargs: object) -> MagicMock:
        assert page.context._skyvern_cdp_download_interceptor_bind_lock.locked()
        return MagicMock(body=refetched_bytes)

    fetch_file_bytes = AsyncMock(side_effect=guarded_fetch)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.url = "https://example.com/presigned/report.pdf"
    download.page = page
    download.save_as = AsyncMock(side_effect=Exception("Target page, context or browser has been closed"))

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "bs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="bs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch(
                "skyvern.webeye.actions.handler.fetch_file_bytes",
                new=fetch_file_bytes,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        landed = sorted(os.listdir(primary_dir))
        landed_bytes = Path(primary_dir, landed[0]).read_bytes() if landed else b""

    assert results[-1].download_triggered is True
    assert action.download_triggered is True
    assert len(landed) == 1 and landed[0].endswith("-report.pdf")
    assert landed_bytes == refetched_bytes
    assert results[-1].downloaded_files == landed
    download.save_as.assert_awaited_once()
    fetch_file_bytes.assert_awaited_once_with(
        download.url,
        headers={"Cookie": "session=authenticated"},
        authorize_request_hop=authorize_request_hop,
        download_scope=None,
        approved_initial_url=download.url,
    )
    page.context._skyvern_cdp_download_interceptor._cookie_header_for_url.assert_awaited_once_with(download.url)
    page.context.request.get.assert_not_called()
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_event_fallback_used"] is True


@pytest.mark.asyncio
async def test_handle_action_adopted_session_falls_through_to_session_folder_when_helper_returns_none(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When the adopted-session save helper cannot land bytes (e.g. blob unsupported, CSP, frame
    detach), the poll loop must still give the browser-session folder sync a chance to detect a
    file the shared browser landed natively, instead of breaking out on the first helper failure.
    """
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id="bs-1",
        download_timeout=10.0,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    page.evaluate = AsyncMock()
    page.expose_binding = AsyncMock()
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    authorize_request_hop = AsyncMock()
    _bind_adopted_download_authorizer(page, authorize_request_hop)
    fetch_file_bytes = AsyncMock(side_effect=Exception("guarded refetch failed"))

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.url = "https://example.com/presigned/report.pdf"
    download.page = page
    download.save_as = AsyncMock(side_effect=Exception("Target page, context or browser has been closed"))

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    # first STORAGE listing (before action) returns empty; subsequent listings return a file
    # that the shared browser landed in its session-scoped download folder.
    session_landed_path = "s3://bucket/browser_sessions/bs-1/downloads/session-late.pdf"
    storage_calls = 0

    async def storage_side_effect(**kwargs: object) -> list[str]:
        nonlocal storage_calls
        storage_calls += 1
        if storage_calls == 1:
            return []
        return [session_landed_path]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "bs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(side_effect=storage_side_effect)
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="bs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch(
                "skyvern.webeye.actions.handler.fetch_file_bytes",
                new=fetch_file_bytes,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    assert action.download_triggered is True
    # helper attempted and failed; recovery came from the browser-session folder poll
    download.save_as.assert_awaited_once()
    fetch_file_bytes.assert_awaited_once_with(
        download.url,
        headers={"Cookie": "session=authenticated"},
        authorize_request_hop=authorize_request_hop,
        download_scope=None,
        approved_initial_url=download.url,
    )
    # storage was polled at least twice: once before the action, again on a later loop iteration
    assert mock_app.STORAGE.list_downloaded_files_in_browser_session.await_count >= 2
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_event_fallback_attempted"] is True
    assert span_attrs["download_event_fallback_used"] is False
    assert span_attrs["download_event_fallback_failed"] is True
    assert span_attrs["download_triggered"] is True


@pytest.mark.asyncio
async def test_handle_action_adopted_session_helper_failure_does_not_short_circuit_observed_files_poll(
    span_exporter: InMemorySpanExporter,
) -> None:
    """Adopted-session save helper failure must not break out of the poll loop. Even when nothing
    ever lands in the browser-session folder, the loop must keep polling until the download budget
    is exhausted, otherwise the primary recovery signal is starved.
    """
    now = datetime.now(UTC)
    organization = make_organization(now)
    # Small, non-flaky budget: enough for a baseline poll plus at least one post-helper poll, while
    # keeping the test fast. The stated contract (keep polling after the helper returns None) is what
    # this test isolates, so the real _save_adopted_session_download is replaced with an instant None
    # below rather than being driven to failure by the outer timeout.
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id="bs-1",
        download_timeout=0.3,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    page.evaluate = AsyncMock()
    page.expose_binding = AsyncMock()
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)
    # The real helper is replaced below with an instant-None stand-in, so the authorizer it would have
    # received is never exercised — but the caller still resolves it via the page's bound interceptor
    # *before* invoking the (mocked) helper, so that binding still needs to be real.
    _bind_adopted_download_authorizer(page, AsyncMock())
    fetch_file_bytes = AsyncMock(side_effect=Exception("connection gone"))

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.url = "https://example.com/presigned/report.pdf"
    download.page = page

    # Isolate the stated contract: the adopted-session save helper returns None (could not save), and
    # the poll loop must keep polling the browser-session folder afterwards. Replacing the real helper
    # keeps this fast and deterministic instead of driving it to failure via the outer timeout.
    save_helper = AsyncMock(return_value=None)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "bs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler._save_adopted_session_download", save_helper),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="bs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch(
                "skyvern.webeye.actions.handler.fetch_file_bytes",
                new=fetch_file_bytes,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is False
    assert action.download_triggered is False
    # The adopted-session save helper was attempted and returned None (could not save).
    save_helper.assert_awaited()
    # the loop kept polling the browser-session folder after the helper returned None,
    # rather than breaking out on the first failed attempt.
    assert mock_app.STORAGE.list_downloaded_files_in_browser_session.await_count >= 2
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_event_fallback_attempted"] is True
    assert span_attrs["download_event_fallback_used"] is False
    assert span_attrs["download_event_fallback_failed"] is True
    assert span_attrs["download_triggered"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("download_timeout", [10.0, None])
async def test_handle_action_adopted_session_xhr_staging_recovered_when_helper_fails(
    span_exporter: InMemorySpanExporter,
    download_timeout: float | None,
) -> None:
    """When the adopted-session helper returns None and XHR staging already has a
    file, it should be moved to the download dir on the same iteration instead of
    waiting for browser-session folder polling or download_timeout."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id="bs-1",
        download_timeout=download_timeout,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    page.evaluate = AsyncMock()
    page.expose_binding = AsyncMock()
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)
    authorize_request_hop = AsyncMock()
    _bind_adopted_download_authorizer(page, authorize_request_hop)
    fetch_file_bytes = AsyncMock(side_effect=Exception("connection gone"))

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.url = "https://example.com/presigned/report.pdf"
    download.page = page
    download.save_as = AsyncMock(side_effect=Exception("Target page, context or browser has been closed"))

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "bs-1")
        os.makedirs(primary_dir)
        staging_dir = os.path.join(temp_root, "staging")
        os.makedirs(staging_dir)
        # pre-populate staging dir with a file the XHR listener captured
        staged = os.path.join(staging_dir, "xhr-captured.pdf")
        with open(staged, "wb") as f:
            f.write(b"%PDF-1.4 xhr staged bytes")

        mock_xhr = MagicMock()
        mock_xhr.enable = MagicMock()
        mock_xhr.drain = AsyncMock()

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=staging_dir),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", return_value=mock_xhr) as capture_cls,
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="bs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch(
                "skyvern.webeye.actions.handler.fetch_file_bytes",
                new=fetch_file_bytes,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        assert results[-1].download_triggered is True
        assert action.download_triggered is True
        capture_cls.assert_called_once_with(
            page,
            Path(staging_dir),
            timeout_seconds=download_timeout if download_timeout is not None else BROWSER_DOWNLOAD_TIMEOUT,
        )
        download.save_as.assert_awaited_once()
        fetch_file_bytes.assert_awaited_once_with(
            download.url,
            headers={"Cookie": "session=authenticated"},
            authorize_request_hop=authorize_request_hop,
            download_scope=None,
            approved_initial_url=download.url,
        )
        page.context.request.get.assert_not_called()
        # file moved from staging to download dir
        landed_files = sorted(os.listdir(primary_dir))
        assert landed_files == ["xhr-captured.pdf"]
        assert os.path.exists(staged) is False
        span_attrs = _download_wait_span_attrs(span_exporter)
        assert span_attrs["download_event_fallback_attempted"] is True
        assert span_attrs["download_event_fallback_used"] is False
        assert span_attrs["download_event_fallback_failed"] is True
        assert span_attrs["download_triggered"] is True


@pytest.mark.asyncio
async def test_handle_action_hard_deadline_drain_uses_zero_remaining_budget_and_moves_completed_staging(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock()
    clock = _FakeMonotonic()
    mock_xhr = MagicMock()
    mock_xhr.has_in_flight_requests = True
    mock_xhr.drain = AsyncMock(return_value=False)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        download_dir = Path(temp_root) / "download"
        download_dir.mkdir()
        staging_dir = Path(temp_root) / "staging"
        staging_dir.mkdir()
        (staging_dir / "completed.pdf").write_bytes(b"%PDF-1.4 completed")
        list_calls = 0

        def list_files(path: Path | str) -> list[str]:
            nonlocal list_calls
            list_calls += 1
            if list_calls == 2:
                clock.current = 30.0
            return [str(item) for item in Path(path).iterdir() if item.is_file()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(download_dir)),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", return_value=mock_xhr),
            patch("skyvern.webeye.actions.handler.time", clock),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["completed.pdf"]
    assert mock_xhr.drain.await_args_list == [call(timeout_seconds=0), call(timeout_seconds=0)]


@pytest.mark.asyncio
async def test_handle_action_xhr_body_finishing_within_remaining_deadline_is_collected() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock()
    clock = _FakeMonotonic()
    mock_xhr = MagicMock()
    mock_xhr.has_in_flight_requests = False

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        download_dir = Path(temp_root) / "download"
        download_dir.mkdir()
        staging_dir = Path(temp_root) / "staging"
        staging_dir.mkdir()
        list_calls = 0

        def list_files(path: Path | str) -> list[str]:
            nonlocal list_calls
            list_calls += 1
            if list_calls == 2:
                clock.current = 5.0
            return [str(item) for item in Path(path).iterdir() if item.is_file()]

        async def drain(timeout_seconds: float | None = None) -> bool:
            if timeout_seconds == 25.0:
                (staging_dir / "slow.pdf").write_bytes(b"%PDF-1.4 slow")
            return True

        mock_xhr.drain = AsyncMock(side_effect=drain)
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(download_dir)),
            patch("skyvern.webeye.actions.handler.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", return_value=mock_xhr),
            patch("skyvern.webeye.actions.handler.time", clock),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["slow.pdf"]
    assert mock_xhr.drain.await_args_list == [call(timeout_seconds=25.0), call(timeout_seconds=0)]


@pytest.mark.asyncio
async def test_handle_action_fast_native_success_cleanup_uses_zero_xhr_budget() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task = task.model_copy(update={"download_timeout": None})
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock()
    mock_xhr = MagicMock()
    mock_xhr.has_in_flight_requests = True
    mock_xhr.drain = AsyncMock(return_value=False)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        download_dir = Path(temp_root) / "download"
        download_dir.mkdir()
        (download_dir / "native.pdf").write_bytes(b"%PDF-1.4 native")
        observed = [[], [str(download_dir / "native.pdf")], [str(download_dir / "native.pdf")]]
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(download_dir)),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=observed),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", return_value=mock_xhr),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    mock_xhr.drain.assert_awaited_once_with(timeout_seconds=0)


_RECOVER_READ = "skyvern.webeye.actions.handler.SkyvernFrame.read_http_url_bytes"


class _RecoveryFrame:
    def __init__(self, iframe_srcs: list) -> None:
        self._srcs = iframe_srcs
        self.evaluate_calls = 0

    async def evaluate(self, expression: str | None = None, arg: object = None) -> list:
        self.evaluate_calls += 1
        return self._srcs


class _RecoveryPage:
    def __init__(self, main_srcs: list, child_frames: list | None = None, frames_include_main: bool = False) -> None:
        self.main_frame = _RecoveryFrame(main_srcs)
        extra = child_frames or []
        # Real Playwright's page.frames already includes the main frame; frames_include_main mirrors that.
        self.frames = [self.main_frame, *extra] if frames_include_main else extra
        # Page-like surface: the main frame is evaluated by passing the Page itself (is_page_like),
        # so the Page delegates evaluate to its main frame. context=None keeps SkyvernFrame.evaluate
        # off the main-world prefix path (no prefix configured in tests).
        self.context = None

    def bring_to_front(self) -> None:
        return None

    async def evaluate(self, expression: str | None = None, arg: object = None) -> list:
        return await self.main_frame.evaluate(expression, arg)


def test_looks_like_pdf_accepts_pdf_and_rejects_others() -> None:
    assert _looks_like_pdf(b"%PDF-1.7\n...statement...")
    assert _looks_like_pdf(b"\xef\xbb\xbf%PDF-1.4 with a preamble")  # exact UTF-8 BOM tolerated
    assert not _looks_like_pdf(b"<!doctype html><html>login</html>")
    assert not _looks_like_pdf(b"<html>your %PDF-1.4 is attached</html>")  # marker not at the start
    assert not _looks_like_pdf(b"")
    # only the exact 3-byte BOM is stripped, not arbitrary members of {0xef,0xbb,0xbf}
    assert not _looks_like_pdf(b"\xef%PDF-1.4")  # a lone 0xef is not a BOM
    assert not _looks_like_pdf(b"\xbb\xbf%PDF-1.4")  # a partial BOM is not a BOM
    assert not _looks_like_pdf(b"\xef\xbb\xbf\xef\xbb\xbf%PDF-")  # only one BOM is stripped


@pytest.mark.asyncio
async def test_collect_inline_iframe_src_candidates_filters_and_dedupes() -> None:
    page = _RecoveryPage(
        main_srcs=[
            "https://host.example/api/StatementPdf?access_token=x",
            "https://host.example/api/StatementPdf?access_token=x",  # duplicate dropped
            "about:blank",
            "blob:https://host.example/abc",
            "http://host.example/other.pdf",
            "",
        ]
    )
    result = await _collect_inline_iframe_src_candidates(page)
    assert result == [
        "https://host.example/api/StatementPdf?access_token=x",
        "http://host.example/other.pdf",
    ]


@pytest.mark.asyncio
async def test_collect_inline_iframe_src_candidates_routes_main_frame_as_page_and_child_as_frame() -> None:
    # Enumeration must go through the unified SkyvernFrame.evaluate seam (which centralizes
    # main-world routing, timeout handling, and navigation-context recovery), not a raw
    # Playwright frame.evaluate. The main frame is routed as the Page so any context-level
    # main-world prefix stays attached; child frames evaluate in-frame (prefixes are page-scoped).
    child = _RecoveryFrame(["https://host.example/b.pdf"])
    page = _RecoveryPage(main_srcs=["https://host.example/a.pdf"], child_frames=[child])
    eval_mock = AsyncMock(return_value=[])
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.evaluate", eval_mock):
        await _collect_inline_iframe_src_candidates(page)

    targets = [c.kwargs["frame"] for c in eval_mock.await_args_list]
    assert targets == [page, child]  # main frame as Page, child as Frame
    assert all(c.kwargs["expression"] == _INLINE_IFRAME_SRC_JS for c in eval_mock.await_args_list)


@pytest.mark.asyncio
async def test_collect_inline_iframe_src_candidates_is_uncapped() -> None:
    # No arbitrary cap: every http(s) iframe src is enumerated so the before/after action-window
    # comparison can't lose a target that sits past many pre-existing iframes.
    srcs = [f"https://host.example/{i}.pdf" for i in range(30)]
    page = _RecoveryPage(main_srcs=srcs)
    result = await _collect_inline_iframe_src_candidates(page)
    assert result == srcs


@pytest.mark.asyncio
async def test_collect_inline_iframe_src_candidates_dedupes_frames_by_identity() -> None:
    # Playwright's page.frames already includes the main frame, so iterating
    # [main_frame, *frames] would evaluate the main frame twice. Frame-identity dedup
    # (via _all_page_frames) evaluates each distinct frame exactly once.
    page = _RecoveryPage(main_srcs=["https://host.example/api/StatementPdf?access_token=x"], frames_include_main=True)
    result = await _collect_inline_iframe_src_candidates(page)
    assert result == ["https://host.example/api/StatementPdf?access_token=x"]
    assert page.main_frame.evaluate_calls == 1


@pytest.mark.asyncio
async def test_recover_blocked_inline_pdf_writes_file(tmp_path: Path) -> None:
    page = _RecoveryPage(main_srcs=["https://host.example/api/StatementPdf?access_token=secret"])
    pdf_bytes = b"%PDF-1.7\n...statement bytes..."
    with patch(_RECOVER_READ, AsyncMock(return_value=pdf_bytes)):
        result = await _recover_blocked_inline_pdf_download(
            page, tmp_path, workflow_run_id="wr_test", iframe_srcs_before=[]
        )

    assert result is not None
    assert result.parent == tmp_path
    assert result.suffix == ".pdf"
    assert result.read_bytes() == pdf_bytes


@pytest.mark.asyncio
async def test_recover_passes_recovery_timeout_to_read_http_url_bytes(tmp_path: Path) -> None:
    # The whole-operation 30s budget (authoritative outer asyncio cap) must not be undercut by the
    # generic ~5s evaluate timeout: recovery reads each candidate with the 30s timeout forwarded
    # into SkyvernFrame.evaluate so a slow-but-alive statement isn't rejected early.
    page = _RecoveryPage(main_srcs=["https://host.example/api/StatementPdf?access_token=x"])
    read_mock = AsyncMock(return_value=b"%PDF-1.7 statement")
    with patch(_RECOVER_READ, read_mock):
        result = await _recover_blocked_inline_pdf_download(
            page, tmp_path, workflow_run_id="wr_test", iframe_srcs_before=[]
        )

    assert result is not None
    assert read_mock.await_args.kwargs["timeout_ms"] == _BLOCKED_INLINE_PDF_RECOVERY_TIMEOUT_SECONDS * 1000
    assert "redirect" not in read_mock.await_args.kwargs


@pytest.mark.asyncio
async def test_recover_honors_download_suffix_naming(tmp_path: Path) -> None:
    """The recovered file rejoins the block-configured download_suffix naming path (the common
    real-world shape, since statement endpoints rarely end in .pdf)."""
    page = _RecoveryPage(main_srcs=["https://host.example/api/StatementPdf?access_token=x"])
    pdf_bytes = b"%PDF-1.7 statement"
    ctx = MagicMock(download_suffix="mystatement", task_id="tsk_x")
    with (
        patch(_RECOVER_READ, AsyncMock(return_value=pdf_bytes)),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=ctx),
    ):
        result = await _recover_blocked_inline_pdf_download(
            page, tmp_path, workflow_run_id="wr_test", iframe_srcs_before=[]
        )

    assert result is not None
    assert result.name == "mystatement.pdf"
    assert result.read_bytes() == pdf_bytes


@pytest.mark.asyncio
async def test_recover_returns_none_without_http_candidate(tmp_path: Path) -> None:
    page = _RecoveryPage(main_srcs=["about:blank", "blob:https://host.example/x"])
    fetch = AsyncMock()
    with patch(_RECOVER_READ, fetch):
        result = await _recover_blocked_inline_pdf_download(page, tmp_path, workflow_run_id=None, iframe_srcs_before=[])

    assert result is None
    fetch.assert_not_awaited()  # nothing http(s) to fetch
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_recover_rejects_non_pdf_bytes(tmp_path: Path) -> None:
    page = _RecoveryPage(main_srcs=["https://host.example/framed"])
    with patch(_RECOVER_READ, AsyncMock(return_value=b"<html>content is blocked</html>")):
        result = await _recover_blocked_inline_pdf_download(page, tmp_path, workflow_run_id=None, iframe_srcs_before=[])

    assert result is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"", None])
async def test_recover_rejects_empty_and_unrecoverable(tmp_path: Path, value: bytes | None) -> None:
    page = _RecoveryPage(main_srcs=["https://host.example/x.pdf"])
    with patch(_RECOVER_READ, AsyncMock(return_value=value)):
        result = await _recover_blocked_inline_pdf_download(page, tmp_path, workflow_run_id=None, iframe_srcs_before=[])

    assert result is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_recover_fails_closed_on_multiple_distinct_new_pdfs(tmp_path: Path) -> None:
    # Two equally-plausible NEW inline PDFs appeared in the action window. Nothing
    # ties either to the click, so recovery must fail closed rather than guess the
    # first one (the #13898 first-PDF-wins bug reproduced in Phase 1's real browser).
    page = _RecoveryPage(
        main_srcs=[
            "https://host.example/api/StatementPdf?access_token=a",
            "https://host.example/api/StatementPdf?access_token=b",
        ]
    )

    async def fake_read(_page: object, url: str, **_kwargs: object) -> bytes:
        return b"%PDF-1.5 statement A" if url.endswith("=a") else b"%PDF-1.5 statement B"

    with patch(_RECOVER_READ, side_effect=fake_read):
        result = await _recover_blocked_inline_pdf_download(page, tmp_path, workflow_run_id=None, iframe_srcs_before=[])

    assert result is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_recover_propagates_cancellation(tmp_path: Path) -> None:
    page = _RecoveryPage(main_srcs=["https://host.example/x.pdf"])
    with patch(_RECOVER_READ, AsyncMock(side_effect=asyncio.CancelledError())):
        with pytest.raises(asyncio.CancelledError):
            await _recover_blocked_inline_pdf_download(page, tmp_path, workflow_run_id=None, iframe_srcs_before=[])

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_recover_shares_one_budget_across_all_candidates(tmp_path: Path) -> None:
    # Several slow candidate fetches share a single external budget (the shape handle_action wraps
    # the call in): the whole operation is cancelled at that one deadline — the budget is not spent
    # per-candidate — and it unwinds cleanly, writing nothing.
    page = _RecoveryPage(main_srcs=[f"https://host.example/api/StatementPdf?doc={i}" for i in range(3)])

    async def slow_read(*_args: object, **_kwargs: object) -> bytes:
        await asyncio.sleep(0.2)
        return b"%PDF-1.5 slow"

    started = time.monotonic()
    with patch(_RECOVER_READ, side_effect=slow_read):
        with pytest.raises(asyncio.TimeoutError):
            async with asyncio.timeout(0.05):
                await _recover_blocked_inline_pdf_download(page, tmp_path, workflow_run_id=None, iframe_srcs_before=[])

    assert time.monotonic() - started < 0.15  # one 0.05s budget, not 3 x 0.2s per candidate
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_recover_excludes_preexisting_pdf_and_recovers_newly_attached(tmp_path: Path) -> None:
    # A benign decoy PDF iframe was already on the page before the click; the click
    # attached the intended statement iframe. Only the newly-attached src is a
    # candidate, so the decoy is never fetched or saved.
    decoy = "https://host.example/api/decoy?doc=advert"
    statement = "https://host.example/api/StatementPdf?access_token=fresh"
    page = _RecoveryPage(main_srcs=[decoy, statement])
    statement_bytes = b"%PDF-1.5 intended statement"
    fetched: list[str] = []

    async def fake_read(_page: object, url: str, **_kwargs: object) -> bytes:
        fetched.append(url)
        return statement_bytes if url == statement else b"%PDF-1.5 DECOY should never be read"

    with patch(_RECOVER_READ, side_effect=fake_read):
        result = await _recover_blocked_inline_pdf_download(
            page, tmp_path, workflow_run_id="wr_test", iframe_srcs_before=[decoy]
        )

    assert result is not None
    assert result.read_bytes() == statement_bytes
    assert fetched == [statement]  # decoy in the baseline was never fetched
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.asyncio
async def test_recover_finds_target_appended_past_many_preexisting_iframes(tmp_path: Path) -> None:
    # A heavy portal with many pre-existing iframes (ads/trackers, plus a decoy PDF) and the intended
    # statement appended LAST, past any arbitrary DOM-order cap. Uncapped enumeration must still see
    # it: the target is recovered and the pre-existing decoy is excluded by the baseline.
    decoy = "https://host.example/ads/decoy.pdf?doc=advert"
    preexisting = [decoy] + [f"https://host.example/tracker/{i}.html" for i in range(12)]
    statement = "https://host.example/api/StatementPdf?access_token=fresh"
    page = _RecoveryPage(main_srcs=[*preexisting, statement])
    statement_bytes = b"%PDF-1.5 intended statement past the cap"
    fetched: list[str] = []

    async def fake_read(_page: object, url: str, **_kwargs: object) -> bytes:
        fetched.append(url)
        return statement_bytes if url == statement else b"%PDF-1.5 decoy" if url == decoy else b"<html>tracker</html>"

    with patch(_RECOVER_READ, side_effect=fake_read):
        result = await _recover_blocked_inline_pdf_download(
            page, tmp_path, workflow_run_id="wr_test", iframe_srcs_before=preexisting
        )

    assert result is not None
    assert result.read_bytes() == statement_bytes
    assert fetched == [statement]  # only the newly-appended src is fetched; decoy/trackers excluded
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.asyncio
async def test_recover_names_extensionless_url_with_pdf_suffix(tmp_path: Path) -> None:
    # A confirmed-PDF candidate whose URL has no path basename (".../?token=...") must still be
    # persisted with a .pdf extension rather than an extension-less generic name.
    src = "https://host.example/?token=abc"
    page = _RecoveryPage(main_srcs=[src])

    with patch(_RECOVER_READ, AsyncMock(return_value=b"%PDF-1.7 statement")):
        result = await _recover_blocked_inline_pdf_download(
            page, tmp_path, workflow_run_id="wr_test", iframe_srcs_before=[]
        )

    assert result is not None
    assert result.suffix == ".pdf"
    assert result.name.endswith(".pdf")


@pytest.mark.asyncio
async def test_recover_fails_closed_when_only_preexisting_pdf_remains(tmp_path: Path) -> None:
    # Same-URL reload: the statement iframe was already present with this exact src
    # before the click and the src is unchanged after. Nothing new appeared in the
    # action window, so recovery fails closed rather than re-saving a stale frame.
    statement = "https://host.example/api/StatementPdf?access_token=stale"
    page = _RecoveryPage(main_srcs=[statement])

    with patch(_RECOVER_READ, AsyncMock(return_value=b"%PDF-1.5 stale")) as fetch:
        result = await _recover_blocked_inline_pdf_download(
            page, tmp_path, workflow_run_id=None, iframe_srcs_before=[statement]
        )

    assert result is None
    fetch.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_recover_recovers_reused_iframe_navigated_to_new_src(tmp_path: Path) -> None:
    # An existing iframe navigated from an old src to a new statement src during the
    # action window. The new src is not in the baseline, so it is recovered.
    old_src = "https://host.example/api/StatementPdf?access_token=old"
    new_src = "https://host.example/api/StatementPdf?access_token=new"
    page = _RecoveryPage(main_srcs=[new_src])
    new_bytes = b"%PDF-1.7 freshly navigated statement"

    with patch(_RECOVER_READ, AsyncMock(return_value=new_bytes)):
        result = await _recover_blocked_inline_pdf_download(
            page, tmp_path, workflow_run_id="wr_test", iframe_srcs_before=[old_src]
        )

    assert result is not None
    assert result.read_bytes() == new_bytes


@pytest.mark.asyncio
async def test_recover_fails_closed_on_two_urls_with_identical_bytes(tmp_path: Path) -> None:
    # Two DISTINCT new candidate URLs both return byte-identical PDFs. They are still
    # two equally-plausible candidates with nothing tying either to the click, so
    # recovery must fail closed — identical bytes are not a license to pick one. (Exact
    # duplicate srcs never reach here; _collect_inline_iframe_src_candidates dedupes by src.)
    a = "https://host.example/api/StatementPdf?doc=1"
    b = "https://host.example/api/StatementPdf?doc=2"
    page = _RecoveryPage(main_srcs=[a, b])
    same_bytes = b"%PDF-1.5 identical document bytes"

    with patch(_RECOVER_READ, AsyncMock(return_value=same_bytes)):
        result = await _recover_blocked_inline_pdf_download(page, tmp_path, workflow_run_id=None, iframe_srcs_before=[])

    assert result is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_recover_requires_explicit_baseline(tmp_path: Path) -> None:
    # The pre-click baseline is mandatory: without it, recovery would fall back to
    # scanning every PDF on the page (the #13898 bug). Omitting it must be a hard error,
    # never a silent global scan.
    page = _RecoveryPage(main_srcs=["https://host.example/api/StatementPdf?access_token=x"])
    with pytest.raises(TypeError):
        await _recover_blocked_inline_pdf_download(page, tmp_path, workflow_run_id=None)


@pytest.mark.asyncio
async def test_handle_action_threads_preclick_iframe_baseline_into_recovery(tmp_path: Path) -> None:
    # Recovery can only exclude pre-existing frames if it receives the iframe srcs
    # captured BEFORE the click. Prove handle_action snapshots the baseline and threads
    # it into _recover_blocked_inline_pdf_download.
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )
    task.download_timeout = 0.01
    baseline = ["https://host.example/preexisting.pdf"]
    seen: dict = {}

    async def fake_recover(_page: object, _dir: object, *, workflow_run_id: object, iframe_srcs_before: object) -> None:
        seen["baseline"] = iframe_srcs_before
        return None

    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE = MagicMock()

    with (
        patch.object(ActionHandler, "_handle_action", new=AsyncMock(return_value=[ActionSuccess()])),
        patch(
            "skyvern.webeye.actions.handler._collect_inline_iframe_src_candidates",
            new=AsyncMock(return_value=baseline),
        ),
        patch("skyvern.webeye.actions.handler._recover_blocked_inline_pdf_download", side_effect=fake_recover),
        patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(tmp_path)),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
        patch(
            "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
            new=AsyncMock(),
        ),
        patch("skyvern.webeye.actions.handler.app", mock_app),
    ):
        results = await asyncio.wait_for(
            ActionHandler.handle_action(scraped_page=scraped_page, task=task, step=step, page=page, action=action),
            timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
        )

    assert seen["baseline"] == baseline
    assert results[-1].download_triggered is False


@pytest.mark.asyncio
async def test_handle_action_skips_recovery_when_native_download_fires(tmp_path: Path) -> None:
    # The blocked-inline recovery is a fallback for when NO download event fired. A
    # normal native download must leave it untouched.
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )
    task.download_timeout = None

    async def mock_inner_handle_action(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        (tmp_path / "report.pdf").write_bytes(b"%PDF-1.7 native")
        return [ActionSuccess()]

    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE = MagicMock()

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
        patch("skyvern.webeye.actions.handler._recover_blocked_inline_pdf_download") as recover,
        patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(tmp_path)),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
        patch(
            "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
            new=AsyncMock(),
        ),
        patch("skyvern.webeye.actions.handler.app", mock_app),
    ):
        results = await asyncio.wait_for(
            ActionHandler.handle_action(scraped_page=scraped_page, task=task, step=step, page=page, action=action),
            timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
        )

    recover.assert_not_called()
    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]


@pytest.mark.asyncio
async def test_handle_action_blocked_inline_recovery_is_time_bounded(tmp_path: Path) -> None:
    # A hung same-origin fetch inside recovery must not out-wait the download loop it backstops.
    # The whole recovery is bounded by one budget; on timeout handle_action falls through to the
    # normal download-not-triggered follow-up rather than hanging or hard-failing.
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )
    task.download_timeout = 0.01
    entered = asyncio.Event()

    async def hung_recovery(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()  # never returns

    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE = MagicMock()

    with (
        patch.object(ActionHandler, "_handle_action", new=AsyncMock(return_value=[ActionSuccess()])),
        patch(
            "skyvern.webeye.actions.handler._collect_inline_iframe_src_candidates",
            new=AsyncMock(return_value=[]),
        ),
        patch("skyvern.webeye.actions.handler._recover_blocked_inline_pdf_download", side_effect=hung_recovery),
        patch("skyvern.webeye.actions.handler._BLOCKED_INLINE_PDF_RECOVERY_TIMEOUT_SECONDS", 0.05),
        patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(tmp_path)),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
        patch(
            "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
            new=AsyncMock(),
        ),
        patch("skyvern.webeye.actions.handler.app", mock_app),
    ):
        # A bound well under the outer wait_for proves handle_action self-bounds the hung recovery.
        results = await asyncio.wait_for(
            ActionHandler.handle_action(scraped_page=scraped_page, task=task, step=step, page=page, action=action),
            timeout=1.0,
        )

    assert entered.is_set()
    assert results[-1].download_triggered is False
    assert results[-1].needs_followup is True
    assert results[-1].followup_message == DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE


def _download_action(now: datetime, download_url: str) -> tuple[DownloadFileAction, ScrapedPage, object, object]:
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=MagicMock(),
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )
    action = DownloadFileAction(
        file_name="downloaded_file.pdf",
        download_url=download_url,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )
    return action, scraped_page, task, step


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "download_url",
    ["file:///etc/passwd", "http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data/"],
)
async def test_handle_download_file_action_refuses_unsafe_download_url(download_url: str) -> None:
    """download_url is model-supplied, so it must clear the same validator GOTO_URL clears."""
    action, scraped_page, task, step = _download_action(datetime.now(UTC), download_url)
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

    page.goto.assert_not_awaited()
    assert len(result) == 1
    assert isinstance(result[0], ActionFailure)


@pytest.mark.asyncio
async def test_handle_download_file_action_navigates_to_validated_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The navigated URL is the validator's return value, not the raw action field."""
    validate = MagicMock(return_value="https://example.test/validated.pdf")
    monkeypatch.setattr("skyvern.webeye.actions.handler.validate_fetch_url", validate)
    action, scraped_page, task, step = _download_action(datetime.now(UTC), "https://example.test/file.pdf")
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

    validate.assert_called_once_with("https://example.test/file.pdf")
    assert page.goto.call_args[0][0] == "https://example.test/validated.pdf"
    assert isinstance(result[0], ActionSuccess)


ADOPTED_BLOB_DOWNLOAD_URL = "blob:https://files.example.org/8c70-60a8e380e78a"


@pytest.mark.asyncio
async def test_handle_action_adopted_popup_blob_download_wired_end_to_end() -> None:
    """Runtime proof of the eager blob path for adopted sessions (SKY-12621).

    Drives the real wiring: page.on("popup", ...) -> popup "download" event -> eager
    maybe_start -> result threaded into the save path -> listener cleanup. It fails if any of
    those links is removed, because fix #1 was correct in isolation yet broken at runtime.
    """
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id="pbs-session",
        download_timeout=30.0,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/statements"
    page.context.browser = None
    page_listeners: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, cb: page_listeners.__setitem__(event, cb)
    _bind_adopted_download_authorizer(page, AsyncMock())

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )
    action = ClickAction(
        element_id="view-document",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    popup_page = MagicMock()
    popup_page.url = "about:blank"
    popup_page.context = page.context
    popup_listeners: dict[str, Callable[[object], None]] = {}
    popup_page.on.side_effect = lambda event, cb: popup_listeners.__setitem__(event, cb)

    download = MagicMock()
    download.suggested_filename = "statement.pdf"
    download.url = ADOPTED_BLOB_DOWNLOAD_URL
    download.page = popup_page
    download.save_as = AsyncMock()

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            # A "View Document" click opens the blob in a popup that mints and downloads it, then closes.
            page_listeners["popup"](popup_page)
            popup_listeners["download"](download)
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch(
                "skyvern.webeye.actions.handler._read_adopted_session_blob_bytes",
                AsyncMock(return_value=b"%PDF-1.4 statement bytes"),
            ) as eager_read,
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )

        saved = sorted(os.listdir(primary_dir))

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files
    assert len(saved) == 1 and saved[0].endswith("statement.pdf")
    # Eager bytes came from the popup owner and short-circuit save_as.
    eager_read.assert_awaited()
    download.save_as.assert_not_awaited()
    # Popup download wiring was registered and torn down.
    assert "popup" in page_listeners
    popup_page.off.assert_any_call("download", popup_listeners["download"])
    page.off.assert_any_call("popup", page_listeners["popup"])


@pytest.mark.asyncio
async def test_handle_action_eager_read_timeout_does_not_starve_fallback() -> None:
    """A stalled eager read must not consume the whole download-wait budget; the save_as fallback
    must still run within its own bounded window (SKY-12621 fix 1)."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id="pbs-session",
        download_timeout=30.0,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/statements"
    page.context.browser = None
    page_listeners: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, cb: page_listeners.__setitem__(event, cb)
    _bind_adopted_download_authorizer(page, AsyncMock())

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )
    action = ClickAction(
        element_id="view-document",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "statement.pdf"
    download.url = ADOPTED_BLOB_DOWNLOAD_URL
    download.page = page

    async def _save_as(target: object) -> None:
        Path(str(target)).write_bytes(b"%PDF-1.4 fallback bytes")

    download.save_as = AsyncMock(side_effect=_save_as)

    async def _hang(*args: object, **kwargs: object) -> bytes:
        await asyncio.Event().wait()
        return b"never"

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            page_listeners["download"](download)
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        mock_app.STORAGE.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])

        started_at = time.monotonic()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler._read_adopted_session_blob_bytes", _hang),
            patch("skyvern.webeye.actions.handler.EAGER_BLOB_READ_TIMEOUT_SECONDS", 0.1),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1", download_suffix=None),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )
        elapsed = time.monotonic() - started_at
        saved = sorted(os.listdir(primary_dir))

    # The stalled eager read timed out fast and the save_as fallback produced the artifact.
    assert elapsed < CI_TEST_RUNAWAY_TIMEOUT_SECONDS
    assert results[-1].download_triggered is True
    download.save_as.assert_awaited()
    assert len(saved) == 1 and saved[0].endswith("statement.pdf")


@pytest.mark.asyncio
async def test_handle_action_managed_session_disables_eager_and_popup_wiring() -> None:
    """The construction-site gate must disable eager capture and skip popup wiring for managed
    (non-adopted) sessions (SKY-12621 fix 4)."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )  # browser_session_id is None
    page_listeners: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, cb: page_listeners.__setitem__(event, cb)

    captured_kwargs: dict[str, object] = {}
    real_capture_cls = _EagerAdoptedBlobCapture

    def _spy(**kwargs: object) -> _EagerAdoptedBlobCapture:
        captured_kwargs.update(kwargs)
        return real_capture_cls(**kwargs)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler._EagerAdoptedBlobCapture", side_effect=_spy),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_EXTENSION_MAX_SECONDS", 0.1),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0.01),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )

    assert captured_kwargs.get("enabled") is False
    # Managed sessions now gain the identity-only download-popup claim recorder, but no popup-download
    # -event wiring: firing the recorded popup listener attaches no download listener to the popup.
    popup_cb = page_listeners.get("popup")
    assert popup_cb is not None
    sentinel_popup = MagicMock()
    popup_cb(sentinel_popup)
    sentinel_popup.on.assert_not_called()


def _blob_download(url: str, suggested_filename: str) -> MagicMock:
    download = MagicMock()
    download.url = url
    download.suggested_filename = suggested_filename
    return download


def _patch_fresh_probe(*, state_observed: bool = True, retained: bool = True):
    """Patch the retention-freshness gate to a fixed verdict so selection-logic tests can reach the
    read path without standing up a real retention Map."""
    return patch(
        "skyvern.webeye.actions.handler.probe_blob_action_freshness",
        new=AsyncMock(return_value=BlobActionFreshness(state_observed=state_observed, retained=retained)),
    )


class TestRecoverAdoptedSessionBlobPdfIframe:
    """Fast guardrail coverage for the live blob: PDF iframe recovery selection logic."""

    @pytest.mark.asyncio
    async def test_non_blob_download_url_returns_none_without_reading(self) -> None:
        page = MagicMock()
        with (
            patch("skyvern.webeye.actions.handler._blob_iframe_src_titles", new=AsyncMock()) as titles,
            patch("skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes", new=AsyncMock()) as read,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("https://x.example/file.pdf", "file.pdf"), "wr"
            )
        assert result is None
        titles.assert_not_awaited()
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_named_match_pdf_iframe_is_recovered(self) -> None:
        page = MagicMock()
        src = "blob:https://portal.example/live-uuid"
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={src: "Statement.pdf"}),
            ),
            _patch_fresh_probe(),
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
                new=AsyncMock(return_value=b"%PDF-1.4 real"),
            ),
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "statement.pdf"), "wr"
            )
        assert result == b"%PDF-1.4 real"

    @pytest.mark.asyncio
    async def test_empty_suggested_filename_fails_closed_without_reading(self) -> None:
        page = MagicMock()
        with (
            patch("skyvern.webeye.actions.handler._blob_iframe_src_titles", new=AsyncMock()) as titles,
            patch("skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes", new=AsyncMock()) as read,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", ""), "wr"
            )
        assert result is None
        titles.assert_not_awaited()
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_iframe_name_mismatch_fails_closed(self) -> None:
        page = MagicMock()
        src = "blob:https://portal.example/live-uuid"
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={src: "Statement.pdf"}),
            ),
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
                new=AsyncMock(return_value=b"%PDF-1.4 real"),
            ) as read,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "different-name.pdf"), "wr"
            )
        assert result is None
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_matching_titles_fail_closed(self) -> None:
        page = MagicMock()
        srcs = {
            "blob:https://portal.example/a-uuid": "Statement.pdf",
            "blob:https://portal.example/b-uuid": "Statement.pdf",
        }
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value=srcs),
            ),
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
                new=AsyncMock(return_value=b"%PDF-1.4 x"),
            ) as read,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "Statement.pdf"), "wr"
            )
        assert result is None
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_pdf_blob_iframe_is_not_recovered(self) -> None:
        page = MagicMock()
        src = "blob:https://portal.example/live-uuid"
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={src: "Statement.pdf"}),
            ),
            _patch_fresh_probe(),
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
                new=AsyncMock(return_value=b"<html>login</html>"),
            ),
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "statement.pdf"), "wr"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_other_origin_blob_iframe_is_ignored(self) -> None:
        page = MagicMock()
        other = "blob:https://ads.example/other-uuid"
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={other: "Ad.pdf"}),
            ),
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
                new=AsyncMock(return_value=b"%PDF-1.4 ad"),
            ) as read,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "statement.pdf"), "wr"
            )
        assert result is None
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_filename_match_selects_named_iframe_not_first(self) -> None:
        page = MagicMock()
        decoy = "blob:https://portal.example/decoy-uuid"
        wanted = "blob:https://portal.example/wanted-uuid"

        async def _read(*, page, blob_url, workflow_run_id, max_size_bytes, probe):  # noqa: ANN001
            return b"%PDF-1.4 " + blob_url.encode()

        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={decoy: "DecoyDoc.pdf", wanted: "AnnualStatement2026.pdf"}),
            ),
            _patch_fresh_probe(),
            patch("skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes", new=_read),
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page,
                _blob_download("blob:https://portal.example/dl-uuid", "AnnualStatement2026.pdf"),
                "wr",
            )
        assert result == b"%PDF-1.4 " + wanted.encode()

    @pytest.mark.asyncio
    async def test_ambiguous_pdfs_without_filename_match_fail_closed(self) -> None:
        page = MagicMock()
        srcs = {
            "blob:https://portal.example/a-uuid": "DocA.pdf",
            "blob:https://portal.example/b-uuid": "DocB.pdf",
        }
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value=srcs),
            ),
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
                new=AsyncMock(return_value=b"%PDF-1.4 x"),
            ),
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "unrelated.pdf"), "wr"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_filename_match_that_is_not_pdf_fails_closed(self) -> None:
        page = MagicMock()
        srcs = {
            "blob:https://portal.example/named-uuid": "Statement.pdf",
            "blob:https://portal.example/other-uuid": "Other.pdf",
        }

        async def _read(*, page, blob_url, workflow_run_id, max_size_bytes, probe):  # noqa: ANN001
            return b"<html>not a pdf</html>" if "named-uuid" in blob_url else b"%PDF-1.4 other"

        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value=srcs),
            ),
            _patch_fresh_probe(),
            patch("skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes", new=_read),
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "Statement.pdf"), "wr"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_filename_match_logs_reason_and_candidate_count(self) -> None:
        page = MagicMock()
        srcs = {
            "blob:https://portal.example/a-uuid": "DocA.pdf",
            "blob:https://portal.example/b-uuid": "DocB.pdf",
        }
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value=srcs),
            ),
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
                new=AsyncMock(return_value=b"%PDF-1.4 x"),
            ),
            patch("skyvern.webeye.actions.handler.LOG") as log,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "unrelated.pdf"), "wr"
            )
        assert result is None
        reason_calls = [c for c in log.info.call_args_list if c.kwargs.get("reason") == "no_filename_title_match"]
        assert len(reason_calls) == 1
        assert reason_calls[0].kwargs["candidate_count"] == 2

    @pytest.mark.asyncio
    async def test_missing_suggested_filename_logs_reason(self) -> None:
        page = MagicMock()
        with (
            patch("skyvern.webeye.actions.handler._blob_iframe_src_titles", new=AsyncMock()) as titles,
            patch("skyvern.webeye.actions.handler.LOG") as log,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", ""), "wr"
            )
        assert result is None
        titles.assert_not_awaited()
        assert any(c.kwargs.get("reason") == "missing_suggested_filename" for c in log.info.call_args_list)

    @pytest.mark.asyncio
    async def test_no_same_origin_candidate_logs_reason(self) -> None:
        page = MagicMock()
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={"blob:https://ads.example/x": "Ad.pdf"}),
            ),
            patch("skyvern.webeye.actions.handler.LOG") as log,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "statement.pdf"), "wr"
            )
        assert result is None
        no_origin = [c for c in log.info.call_args_list if c.kwargs.get("reason") == "no_same_origin_blob_iframe"]
        assert len(no_origin) == 1
        assert no_origin[0].kwargs["candidate_count"] == 0

    @pytest.mark.asyncio
    async def test_duplicate_match_logs_reason_and_counts(self) -> None:
        page = MagicMock()
        srcs = {
            "blob:https://portal.example/a-uuid": "Statement.pdf",
            "blob:https://portal.example/b-uuid": "Statement.pdf",
        }
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value=srcs),
            ),
            patch("skyvern.webeye.actions.handler.LOG") as log,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "Statement.pdf"), "wr"
            )
        assert result is None
        dup = [c for c in log.warning.call_args_list if c.kwargs.get("reason") == "duplicate_filename_match"]
        assert len(dup) == 1
        assert dup[0].kwargs["match_count"] == 2
        assert dup[0].kwargs["candidate_count"] == 2

    @pytest.mark.asyncio
    async def test_page_access_error_during_discovery_fails_closed(self) -> None:
        # Simulate the page/frame being torn down (e.g. remote session closing) mid-discovery.
        page = MagicMock()
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(side_effect=RuntimeError("Target page, context or browser has been closed")),
            ),
            patch("skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes", new=AsyncMock()) as read,
            patch("skyvern.webeye.actions.handler.LOG") as log,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "Statement.pdf"), "wr"
            )
        assert result is None
        read.assert_not_awaited()
        errors = [c for c in log.warning.call_args_list if c.kwargs.get("reason") == "recovery_error"]
        assert len(errors) == 1
        assert errors[0].kwargs["error_type"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_read_error_during_recovery_fails_closed(self) -> None:
        # A matched candidate whose read raises must fail closed, not propagate.
        page = MagicMock()
        src = "blob:https://portal.example/live-uuid"
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={src: "Statement.pdf"}),
            ),
            _patch_fresh_probe(),
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
                new=AsyncMock(side_effect=RuntimeError("frame detached")),
            ),
            patch("skyvern.webeye.actions.handler.LOG") as log,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "Statement.pdf"), "wr"
            )
        assert result is None
        assert any(c.kwargs.get("reason") == "recovery_error" for c in log.warning.call_args_list)

    @pytest.mark.asyncio
    async def test_recovery_does_not_swallow_cancellation(self) -> None:
        # CancelledError (BaseException) must propagate so the enclosing timeout/cancel scope observes it.
        page = MagicMock()
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            patch("skyvern.webeye.actions.handler.LOG"),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _recover_adopted_session_blob_pdf_iframe(
                    page, _blob_download("blob:https://portal.example/dl-uuid", "Statement.pdf"), "wr"
                )

    @pytest.mark.asyncio
    async def test_stale_named_iframe_not_action_fresh_fails_closed(self) -> None:
        # Title matches but the candidate blob is absent from the live retention Map: a lingering
        # same-named iframe from an earlier action must never be saved.
        page = MagicMock()
        src = "blob:https://portal.example/stale-uuid"
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={src: "Statement.pdf"}),
            ),
            patch(
                "skyvern.webeye.actions.handler.probe_blob_action_freshness",
                new=AsyncMock(return_value=BlobActionFreshness(state_observed=True, retained=False)),
            ),
            patch("skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes", new=AsyncMock()) as read,
            patch("skyvern.webeye.actions.handler.LOG") as log,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "Statement.pdf"), "wr"
            )
        assert result is None
        read.assert_not_awaited()
        assert any(c.kwargs.get("reason") == "not_action_fresh" for c in log.info.call_args_list)

    @pytest.mark.asyncio
    async def test_retention_state_unobservable_fails_closed(self) -> None:
        # No probe realm exposes retention state at all: fail closed, distinctly from not_action_fresh.
        page = MagicMock()
        src = "blob:https://portal.example/live-uuid"
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={src: "Statement.pdf"}),
            ),
            patch(
                "skyvern.webeye.actions.handler.probe_blob_action_freshness",
                new=AsyncMock(return_value=BlobActionFreshness(state_observed=False, retained=False)),
            ),
            patch("skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes", new=AsyncMock()) as read,
            patch("skyvern.webeye.actions.handler.LOG") as log,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "Statement.pdf"), "wr"
            )
        assert result is None
        read.assert_not_awaited()
        assert any(c.kwargs.get("reason") == "retention_state_unobservable" for c in log.info.call_args_list)

    @pytest.mark.asyncio
    async def test_action_fresh_named_iframe_is_recovered(self) -> None:
        # The candidate blob is a live key in the retention Map: proceed to the PDF read and save.
        page = MagicMock()
        src = "blob:https://portal.example/live-uuid"
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={src: "Statement.pdf"}),
            ),
            _patch_fresh_probe(),
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.read_blob_url_bytes",
                new=AsyncMock(return_value=b"%PDF-1.4 fresh"),
            ),
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", "Statement.pdf"), "wr"
            )
        assert result == b"%PDF-1.4 fresh"

    @pytest.mark.asyncio
    async def test_freshness_gate_skip_logs_no_sensitive_values(self) -> None:
        # The gate's skip log must carry only a reason + workflow id: never the blob URL, title,
        # suggested filename, or origin/domain.
        page = MagicMock()
        src = "blob:https://portal.example/secret-uuid"
        suggested = "AnnualStatement.pdf"
        with (
            patch(
                "skyvern.webeye.actions.handler._blob_iframe_src_titles",
                new=AsyncMock(return_value={src: suggested}),
            ),
            patch(
                "skyvern.webeye.actions.handler.probe_blob_action_freshness",
                new=AsyncMock(return_value=BlobActionFreshness(state_observed=True, retained=False)),
            ),
            patch("skyvern.webeye.actions.handler.LOG") as log,
        ):
            result = await _recover_adopted_session_blob_pdf_iframe(
                page, _blob_download("blob:https://portal.example/dl-uuid", suggested), "wr"
            )
        assert result is None
        for log_call in log.mock_calls:
            rendered = repr(log_call)
            assert src not in rendered
            assert suggested not in rendered
            assert "portal.example" not in rendered


async def _run_download_action_for_wiring(
    *,
    browser_session_id: str | None,
    has_interceptor: bool = False,
    inner_side_effect: Callable | None = None,
    action=None,
) -> dict:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, default_action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    action = action if action is not None else default_action
    task = task.model_copy(update={"download_timeout": 0.01, "browser_session_id": browser_session_id})

    call_log: list[str] = []

    async def _install(*_args: object, **_kwargs: object) -> None:
        call_log.append("install")

    async def _teardown(*_args: object, **_kwargs: object) -> None:
        call_log.append("teardown")

    async def _inner(*args: object, **kwargs: object):
        call_log.append("handle_action")
        if inner_side_effect is not None:
            return await inner_side_effect(*args, **kwargs)
        return [ActionSuccess()]

    install = AsyncMock(side_effect=_install)
    teardown = AsyncMock(side_effect=_teardown)
    captured_exc: BaseException | None = None

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        storage = MagicMock()
        storage.list_downloading_files_in_browser_session = AsyncMock(return_value=[])
        storage.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])
        mock_app.STORAGE = storage

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=_inner),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.install_blob_url_retention", install, create=True),
            patch("skyvern.webeye.actions.handler.teardown_blob_url_retention", teardown),
            patch(
                "skyvern.webeye.actions.handler.has_download_interceptor_for_context",
                return_value=has_interceptor,
                create=True,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            try:
                await ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )
            except BaseException as exc:  # noqa: BLE001 - teardown-on-failure is under test
                captured_exc = exc
    return {
        "install": install,
        "teardown": teardown,
        "page": page,
        "call_log": call_log,
        "exc": captured_exc,
    }


@pytest.mark.asyncio
async def test_browser_session_download_arms_retention_before_action_and_tears_down() -> None:
    result = await _run_download_action_for_wiring(browser_session_id="pbs-1")

    result["install"].assert_awaited_once()
    result["teardown"].assert_awaited_once()
    assert result["teardown"].await_args.args[0] is result["page"]
    # Retention must be installed before the interaction that mints the blob, otherwise a page that
    # revokes synchronously at click has already dropped the object URL by the time it is patched.
    assert result["call_log"].index("install") < result["call_log"].index("handle_action")


@pytest.mark.asyncio
async def test_interceptor_bound_context_arms_retention_when_no_browser_session() -> None:
    result = await _run_download_action_for_wiring(browser_session_id=None, has_interceptor=True)

    result["install"].assert_awaited_once()
    result["teardown"].assert_awaited_once()
    assert result["call_log"].index("install") < result["call_log"].index("handle_action")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        pytest.param(
            SelectOptionAction(
                element_id="download-select",
                option=SelectOption(label="statement", value="statement"),
                download=True,
            ),
            id="select-option",
        ),
        pytest.param(
            DownloadFileAction(file_name="statement.pdf", download=True),
            id="download-file",
        ),
    ],
)
async def test_typed_download_actions_arm_retention_beyond_click(action) -> None:
    # The arming gate must cover the full trigger_download_action set, not just ClickAction.
    result = await _run_download_action_for_wiring(browser_session_id="pbs-1", action=action)

    result["install"].assert_awaited_once()
    result["teardown"].assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_session_download_does_not_touch_blob_retention() -> None:
    result = await _run_download_action_for_wiring(browser_session_id=None, has_interceptor=False)

    result["install"].assert_not_awaited()
    result["teardown"].assert_not_awaited()


@pytest.mark.asyncio
async def test_retention_torn_down_when_action_raises() -> None:
    async def _boom(*_args: object, **_kwargs: object):
        raise RuntimeError("click failed mid-action")

    result = await _run_download_action_for_wiring(
        browser_session_id=None, has_interceptor=True, inner_side_effect=_boom
    )

    assert isinstance(result["exc"], RuntimeError)
    result["install"].assert_awaited_once()
    result["teardown"].assert_awaited_once()


@pytest.mark.asyncio
async def test_retention_torn_down_on_cancelled() -> None:
    async def _cancel(*_args: object, **_kwargs: object):
        raise asyncio.CancelledError()

    result = await _run_download_action_for_wiring(
        browser_session_id=None, has_interceptor=True, inner_side_effect=_cancel
    )

    assert isinstance(result["exc"], asyncio.CancelledError)
    result["install"].assert_awaited_once()
    result["teardown"].assert_awaited_once()


class _FakeActionDownloadObservation:
    """Neutral provider observation: drops completed remote files into the run dir on its first poll,
    honoring the ``deadline`` (monotonic) contract strictly (a handler passing per-call ``timeout_seconds``
    materializes nothing -- the anti-terminal-cleanup RED)."""

    def __init__(self, files: list[tuple[str, bytes]]) -> None:
        self._pending = list(files)
        self.poll_deadlines: list[float] = []

    async def poll_and_materialize(self, *, destination_dir: Path, deadline: float) -> None:
        self.poll_deadlines.append(deadline)
        destination_dir.mkdir(parents=True, exist_ok=True)
        for name, data in self._pending:
            (destination_dir / name).write_bytes(data)
        self._pending = []


class _RaisingActionDownloadObservation:
    """Provider observation whose poll raises a schema/validation error embedding a presigned URL, mirroring
    the real leak surface (the vendor list body is parsed into typed rows). When ``materialize_first`` is
    set, the first poll drops a completed file (so the handler reaches its final poll) and a later poll raises."""

    def __init__(self, error: Exception, *, materialize_first: tuple[str, bytes] | None = None) -> None:
        self._error = error
        self._materialize_first = materialize_first
        self.calls = 0

    async def poll_and_materialize(self, *, destination_dir: Path, deadline: float) -> None:
        self.calls += 1
        if self._materialize_first is not None and self.calls == 1:
            destination_dir.mkdir(parents=True, exist_ok=True)
            name, data = self._materialize_first
            (destination_dir / name).write_bytes(data)
            return
        raise self._error


class _StallingActionDownloadObservation:
    """Provider observation whose poll never returns on its own -- only the handler's deadline can end it, so
    if the handler polls it once a local artifact already accounts for the action the stalled provider would
    consume the whole download deadline; the fix must never poll it in that case."""

    def __init__(self) -> None:
        self.poll_calls = 0

    async def poll_and_materialize(self, *, destination_dir: Path, deadline: float) -> None:
        self.poll_calls += 1
        await asyncio.Event().wait()


class _ActionDownloadSource:
    """Neutral provider source: hands back a fixed observation and records begin bookkeeping."""

    def __init__(self, observation: object, *, begin_error: Exception | None = None) -> None:
        self._observation = observation
        self._begin_error = begin_error
        self.begin_calls = 0
        self.begin_deadline: float | None = None

    async def begin_observation(self, *, deadline: float) -> object:
        self.begin_calls += 1
        self.begin_deadline = deadline
        if self._begin_error is not None:
            raise self._begin_error
        return self._observation


def _secret_bearing_validation_error(presigned_url: str) -> ValidationError:
    class _Row(BaseModel):
        size: int

    try:
        _Row.model_validate({"size": presigned_url})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


@contextlib.contextmanager
def _capture_handler_logs() -> Iterator[io.StringIO]:
    import skyvern.webeye.actions.handler as handler_module

    buf = io.StringIO()
    previous_config = structlog.get_config()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    saved_log = handler_module.LOG
    handler_module.LOG = structlog.get_logger()
    try:
        yield buf
    finally:
        handler_module.LOG = saved_log
        structlog.configure(**previous_config)


async def _run_download_action_with_provider_source(
    source: _ActionDownloadSource, *, inner: Callable[[Path], None] | None = None
) -> tuple[list[object], object]:
    """Drive ``handle_action`` with ``source`` wired as the vendor download seam.

    ``inner`` (given the run dir) simulates whatever the click itself produces locally before the
    provider poll runs; by default the click yields no local file and no download event.
    """
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    task.download_timeout = 0.2

    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])
    browser_state.browser_artifacts.get_action_download_source = MagicMock(return_value=source)

    with tempfile.TemporaryDirectory() as temp_dir:

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            if inner is not None:
                inner(Path(temp_dir))
            return [ActionSuccess()]

        settle = AsyncMock()
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.get_run_temp_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=settle,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                ),
                timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
            )
    return results, action


# Short enough that pydantic embeds it verbatim (long inputs are truncated), so the full presigned URL is
# present in the exception the handler catches.
_PRESIGNED_URL = "https://s3.aws/f?sig=SECRETSIGMARKER"


@pytest.mark.asyncio
async def test_handle_action_credits_provider_download_materialized_during_poll() -> None:
    # PBS-like timing for a provider-owned download: with a pre-click baseline frozen, a completed remote file
    # appears only during the post-action poll (no download event, no cleanup), and the SAME action is credited.
    observation = _FakeActionDownloadObservation([("report.pdf", b"ready")])
    source = _ActionDownloadSource(observation)
    results, action = await _run_download_action_with_provider_source(source)

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == action.downloaded_files == ["report.pdf"]
    # Baseline was frozen exactly once before the click.
    assert source.begin_calls == 1
    assert source.begin_deadline is not None
    # Every provider poll shared one monotonic deadline (no fresh per-call full timeout).
    assert observation.poll_deadlines
    assert all(isinstance(d, float) for d in observation.poll_deadlines)
    assert len(set(observation.poll_deadlines)) == 1


@pytest.mark.asyncio
async def test_baseline_failure_never_logs_secret_bearing_url_and_stays_fail_open() -> None:
    # begin_observation parses the vendor list into typed rows; a schema error there carries the raw presigned
    # URL. The baseline catch must log only error_type and stay fail-open (provider-diff off, local download counts).
    error = _secret_bearing_validation_error(_PRESIGNED_URL)
    assert "SECRETSIGMARKER" in str(error)  # the exception itself carries the secret
    source = _ActionDownloadSource(_FakeActionDownloadObservation([]), begin_error=error)

    with _capture_handler_logs() as buf:
        results, action = await _run_download_action_with_provider_source(
            source, inner=lambda run_dir: (run_dir / "local.pdf").write_bytes(b"local")
        )

    rendered = buf.getvalue()
    assert source.begin_calls == 1
    assert "Provider download baseline unavailable" in rendered  # the failure is still surfaced
    assert "ValidationError" in rendered  # as non-secret metadata (error_type)
    assert _PRESIGNED_URL not in rendered
    assert "SECRETSIGMARKER" not in rendered
    assert "s3.aws" not in rendered
    # Fail-open: the failed baseline never disturbs the existing local download path.
    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == action.downloaded_files == ["local.pdf"]


@pytest.mark.asyncio
async def test_baseline_cancellation_propagates() -> None:
    # A CancelledError from begin_observation must propagate unchanged, never be swallowed by the catch.
    source = _ActionDownloadSource(_FakeActionDownloadObservation([]), begin_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _run_download_action_with_provider_source(source)


@pytest.mark.asyncio
async def test_mid_poll_failure_never_logs_secret_bearing_url() -> None:
    # A schema/validation error while paging the provider list embeds the raw presigned URL. The mid-poll
    # catch must record only non-secret metadata (error_type), never the exception/traceback, so it can't leak.
    error = _secret_bearing_validation_error(_PRESIGNED_URL)
    assert "SECRETSIGMARKER" in str(error)  # the exception itself carries the secret
    observation = _RaisingActionDownloadObservation(error)
    source = _ActionDownloadSource(observation)

    with _capture_handler_logs() as buf:
        await _run_download_action_with_provider_source(source)

    rendered = buf.getvalue()
    assert observation.calls >= 1
    assert "Provider download poll failed" in rendered  # the failure is still surfaced
    assert "ValidationError" in rendered  # as non-secret metadata (error_type)
    assert _PRESIGNED_URL not in rendered
    assert "SECRETSIGMARKER" not in rendered
    assert "s3.aws" not in rendered


class _RampingMonotonic:
    """A monotonic clock that advances a fixed step on every read, so the download-event grace elapses
    deterministically without real sleeps. Non-monotonic attributes defer to the real ``time`` module."""

    def __init__(self, step: float) -> None:
        self._t = 0.0
        self._step = step

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)

    def monotonic(self) -> float:
        current = self._t
        self._t += self._step
        return current


@pytest.mark.asyncio
async def test_final_poll_failure_never_logs_secret_bearing_url() -> None:
    # download_triggered arrives with NO local artifact: a download event fires but persists empty, so the run
    # dir stays empty -- the "signal but no artifact" case the finalize poll must service. Here it raises the secret.
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/download",
    )
    # Keep the hard timeout far above the event grace (min(60, timeout)) so the empty-persist path fires.
    task.download_timeout = 3600.0

    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])

    error = _secret_bearing_validation_error(_PRESIGNED_URL)
    observation = _RaisingActionDownloadObservation(error)
    source = _ActionDownloadSource(observation)
    browser_state.browser_artifacts.get_action_download_source = MagicMock(return_value=source)

    clock = _RampingMonotonic(step=100.0)

    with tempfile.TemporaryDirectory() as temp_dir:

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            # Fire the browser download event the handler registered before the inner action, so a
            # download signal exists without any file landing in the run directory.
            for call in page.on.call_args_list:
                if call.args and call.args[0] == "download":
                    call.args[1](_download())
                    break
            return [ActionSuccess()]

        settle = AsyncMock()
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        with _capture_handler_logs() as buf:
            with (
                patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
                patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
                patch("skyvern.webeye.actions.handler.get_run_temp_dir", return_value=temp_dir),
                patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
                patch("skyvern.webeye.actions.handler.time", clock),
                patch(
                    "skyvern.webeye.actions.handler._persist_captured_download",
                    new=AsyncMock(return_value=SimpleNamespace(path=None, outcome="empty")),
                ),
                patch(
                    "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                    new=settle,
                ),
                patch("skyvern.webeye.actions.handler.app", mock_app),
            ):
                results = await asyncio.wait_for(
                    ActionHandler.handle_action(
                        scraped_page=scraped_page,
                        task=task,
                        step=step,
                        page=page,
                        action=action,
                    ),
                    timeout=CI_TEST_RUNAWAY_TIMEOUT_SECONDS,
                )

    rendered = buf.getvalue()
    assert results[-1].download_triggered is True  # a signal existed with no local artifact
    assert observation.calls >= 2  # the mid-wait poll and the finalize poll both ran and raised
    assert "Final provider download poll failed" in rendered
    assert "ValidationError" in rendered
    assert _PRESIGNED_URL not in rendered
    assert "SECRETSIGMARKER" not in rendered
    assert "s3.aws" not in rendered


@pytest.mark.asyncio
async def test_local_signal_skips_provider_poll_and_stalled_provider_cannot_consume_deadline() -> None:
    # The existing CDP/local path saves the file before the wait loop runs. The handler must evaluate that local
    # delta first and never poll the provider (a stalled one cannot consume the deadline); the local file is credited.
    observation = _StallingActionDownloadObservation()
    source = _ActionDownloadSource(observation)
    results, action = await _run_download_action_with_provider_source(
        source, inner=lambda run_dir: (run_dir / "local.pdf").write_bytes(b"local")
    )

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == action.downloaded_files == ["local.pdf"]
    assert observation.poll_calls == 0


@pytest.mark.asyncio
async def test_local_artifact_skips_provider_at_finalization_without_duplicate() -> None:
    # A local artifact already accounts for the action before finalization, so the finalize-time provider poll
    # must be skipped -- else a second materialization credits an extra, collision-suffixed copy (a distinct file).
    observation = _FakeActionDownloadObservation([("provider_extra.pdf", b"provider")])
    source = _ActionDownloadSource(observation)
    results, action = await _run_download_action_with_provider_source(
        source, inner=lambda run_dir: (run_dir / "report.pdf").write_bytes(b"local")
    )

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == action.downloaded_files == ["report.pdf"]
    assert observation.poll_deadlines == []


@pytest.mark.asyncio
async def test_explicit_download_marker_popup_recorded_as_claim(tmp_path: Path) -> None:
    """SKY-15371 follow-up, deployed-#16476 reproduction: an explicit ``action.download=True`` click on
    a run with no ``browser_session_id`` opens a never-committed ``":"`` popup and mints no Playwright
    download event, so the action returns ``download_triggered=False`` and the popup is left open. The
    exact popup Page must be recorded as a task-scoped claim so a download credited later (via the CDP
    monitor / file-scan task lifecycle, which never fires a Playwright popup download event) can close
    it. Pre-fix the only popup recorder was gated behind ``browser_session_id``, so nothing was recorded
    for these dynamic-CDP runs and the marker popup leaked into the next task's working-page selection."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.test/documents",
    )
    page.expose_binding = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])
    page.context._skyvern_cdp_download_active = False

    popup_callbacks: list[Callable] = []

    def _capture_on(event: str, callback: Callable) -> None:
        if event == "popup":
            popup_callbacks.append(callback)

    page.on.side_effect = _capture_on

    marker_popup = MagicMock(url=":")
    marker_popup.is_closed.return_value = False
    marker_popup.close = AsyncMock()

    async def click_opens_marker_popup(*args: object, **kwargs: object) -> list[ActionSuccess]:
        # The click opens the download popup; no Playwright download event ever fires on it.
        for callback in popup_callbacks:
            callback(marker_popup)
        return [ActionSuccess()]

    ctx = SkyvernContext(
        task_id=task.task_id,
        workflow_run_id=task.workflow_run_id,
        organization_id=organization.organization_id,
    )
    xhr_capture = MagicMock(has_in_flight_requests=False)
    xhr_capture.drain = AsyncMock(return_value=False)
    mock_app = MagicMock()
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    mock_app.STORAGE = MagicMock()

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=click_opens_marker_popup),
        patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0),
        patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(tmp_path)),
        patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
        patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", return_value=xhr_capture),
        patch(
            "skyvern.webeye.actions.handler._recover_blocked_inline_pdf_download",
            new=AsyncMock(return_value=None),
        ),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=ctx),
        patch("skyvern.webeye.actions.handler.app", mock_app),
    ):
        results = await ActionHandler.handle_action(
            scraped_page=scraped_page,
            task=task,
            step=step,
            page=page,
            action=action,
        )

    assert results[-1].download_triggered is False
    # handle_action itself must not close the popup; closing is deferred to the durable credit seam.
    marker_popup.close.assert_not_called()
    claims = ctx.download_popup_claims.get(task.task_id, [])
    assert any(candidate is marker_popup for candidate in claims), (
        "explicit-download marker popup must be recorded as a task-scoped claim"
    )
