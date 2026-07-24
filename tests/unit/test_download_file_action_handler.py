import asyncio
import os
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.constants import BROWSER_DOWNLOAD_TIMEOUT
from skyvern.errors.errors import UserDefinedError
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import ClickAction, DownloadFileAction
from skyvern.webeye.actions.handler import (
    DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE,
    ActionHandler,
    ScopedXhrDownloadCapture,
    _cleanup_captured_download_popup,
    _persist_captured_download,
    _remove_download_listener,
    handle_download_file_action,
)
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import make_organization, make_step, make_task


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

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=inner),
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(tmp_path)),
        patch("skyvern.webeye.actions.handler.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.05),
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

    def monotonic(self) -> float:
        return self.current


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
    return task, step, page, browser_state, scraped_page, action


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
    interceptor = CDPDownloadInterceptor()
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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging_dir),
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
                    timeout=0.5,
                )

        assert time.monotonic() - started_at < 0.2
        assert not interceptor._browser_download_tasks


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

    with tempfile.TemporaryDirectory() as temp_dir:

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            with open(os.path.join(temp_dir, "report.pdf"), "wb") as file:
                file.write(b"ready")
            return [ActionSuccess()]

        async def slow_download_completion(**kwargs: object) -> None:
            assert kwargs["timeout"] == 0.2
            await asyncio.sleep(0.05)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        started_at = time.monotonic()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_MAX_WAIT_TIME", 0.02),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_TIMEOUT", 0.2),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(side_effect=slow_download_completion),
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
                timeout=0.5,
            )

        elapsed = time.monotonic() - started_at

    assert elapsed >= 0.05
    assert elapsed < 0.5
    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]


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
                timeout=0.5,
            )

    settle.assert_awaited_once()
    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]
    assert "report.pdf.crdownload" not in results[-1].downloaded_files


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
                timeout=0.5,
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
                timeout=0.5,
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
                timeout=0.5,
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
                    timeout=0.5,
                )

        elapsed = time.monotonic() - started_at

    assert elapsed < 0.2


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

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
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

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
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

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
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

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
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
    assert 0 <= span_attrs["download_signal_elapsed_seconds"] < 1


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

    assert elapsed < 1.0
    assert results[-1].download_triggered is False
    assert action.download_triggered is False
    assert results[-1].needs_followup is True
    assert results[-1].followup_message == DOWNLOAD_NOT_TRIGGERED_FOLLOWUP_MESSAGE
    assert wait_for_downloads.await_count == 0
    page.off.assert_called_once()
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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=str(staging_dir)),
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

    assert elapsed < 1.0
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
    page.off.assert_called_once()
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

    async def expose_error_after_grace() -> None:
        await asyncio.sleep(0.04)
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
        await asyncio.gather(*late_tasks)

    assert isinstance(results[-1], ActionFailure)
    assert [error.error_code for error in action.errors or []] == ["download_failed"]
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

    async def finish_request_after_grace() -> None:
        await asyncio.sleep(0.04)
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

    assert late_event_finished.is_set()
    assert isinstance(results[-1], ActionFailure)
    assert [error.error_code for error in action.errors or []] == ["download_failed"]
    assert action.terminal_user_errors is True
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

    async def expose_error_after_grace() -> None:
        await asyncio.sleep(0.04)
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

    assert isinstance(results[-1], ActionFailure)
    assert [error.error_code for error in action.errors or []] == ["download_failed"]
    assert action.terminal_user_errors is True
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

    assert elapsed < 0.1
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

    # Keep a generous wall-clock runaway guard for loaded CI runners; the
    # precise logical deadline is asserted via timeout_seconds below.
    assert 0.03 <= elapsed < 1.0
    assert results[-1].download_triggered is False
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["no_signal_grace_seconds"] == 0.01
    assert span_attrs["timeout_seconds"] == 0.04
    assert span_attrs["download_wait_extended_for_in_flight_request"] is True


@pytest.mark.asyncio
async def test_handle_action_download_cancellation_cleans_extended_wait_listeners() -> None:
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

    async def never_resolving_body() -> bytes:
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
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=str(staging_dir)),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", side_effect=make_capture),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch("skyvern.webeye.actions.handler.app", mock_app),
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
            await asyncio.sleep(0.04)
            assert not handle_task.done()
            handle_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handle_task

        assert body_cancelled.is_set()
        assert captures[0]._response_tasks == set()
        assert captures[0]._drained.is_set()
        assert not staging_dir.exists()
        assert not (staging_dir / "report.pdf").exists()

    for event in ("response", "request", "requestfinished", "requestfailed"):
        page.remove_listener.assert_any_call(event, callbacks[event])
    page.context.remove_listener.assert_called_once_with("page", page.context.on.call_args.args[1])


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
    page.off.assert_called_once_with("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert 0 <= span_attrs["download_signal_elapsed_seconds"] < 1


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
    page.off.assert_called_once_with("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert 0 <= span_attrs["download_signal_elapsed_seconds"] < 1


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
    page.off.assert_called_once_with("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert 0 <= span_attrs["download_signal_elapsed_seconds"] < 1


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

    assert elapsed < 1.0
    assert results[-1].download_triggered is False
    assert action.download_triggered is False
    assert wait_for_downloads.await_count == 0
    download.save_as.assert_awaited_once()
    page.off.assert_called_once_with("download", download_callbacks["download"])
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
    page.off.assert_called_once()


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

    page.off.assert_called_once_with("download", download_callbacks["download"])


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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
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
async def test_handle_action_adopted_session_lands_download_via_eager_save(
    span_exporter: InMemorySpanExporter,
) -> None:
    """On an adopted persistent session the run connection saves the download eagerly at event
    time and the bytes land in the run dir."""
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
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        landed_files = sorted(os.listdir(primary_dir))

    assert results[-1].download_triggered is True
    assert action.download_triggered is True
    assert len(landed_files) == 1 and landed_files[0].endswith("-report.pdf")
    assert results[-1].downloaded_files == landed_files
    download.save_as.assert_awaited_once()
    # eager save wins; the url re-fetch is not needed when save_as succeeds
    page.context.request.get.assert_not_called()
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_event_fallback_used"] is True


@pytest.mark.asyncio
async def test_handle_action_adopted_session_refetches_when_save_as_target_closed(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When the worker tears the shared browser down and the run's save_as raises while the run
    request context is still alive, the adopted-session path re-fetches the replayable url and the
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

    refetched_bytes = b"%PDF-1.4 refetched report bytes"
    refetch_response = MagicMock()
    refetch_response.status = 200
    refetch_response.body = AsyncMock(return_value=refetched_bytes)
    page.context.request.get = AsyncMock(return_value=refetch_response)

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
    page.context.request.get.assert_awaited_once_with(download.url)
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

    # helper save_as raises and refetch returns non-200 → helper returns None
    failed_refetch = MagicMock()
    failed_refetch.status = 403
    failed_refetch.body = AsyncMock(return_value=b"forbidden")
    page.context.request.get = AsyncMock(return_value=failed_refetch)

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
    page.context.request.get.assert_awaited_once_with(download.url)
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
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id="bs-1",
        download_timeout=0.01,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    page.evaluate = AsyncMock()
    page.expose_binding = AsyncMock()
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)
    page.context.request.get = AsyncMock(side_effect=Exception("connection gone"))

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
    download.save_as.assert_awaited_once()
    # the loop kept polling the browser-session folder after the helper failure,
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
    page.context.request.get = AsyncMock(side_effect=Exception("connection gone"))

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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging_dir),
            patch("skyvern.webeye.actions.handler.ScopedXhrDownloadCapture", return_value=mock_xhr) as capture_cls,
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
        page.context.request.get.assert_awaited_once_with(download.url)
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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=str(staging_dir)),
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
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=str(staging_dir)),
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
