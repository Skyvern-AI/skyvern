"""Worker-side CDP frame publisher for reused-CDP / remote browser contexts the in-process screencast cannot reach."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_skyvern_temp_dir

if TYPE_CHECKING:
    from playwright.async_api import CDPSession, Page

    from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()


DEFAULT_CAPTURE_INTERVAL_SECONDS: float = 1.0


def _write_frame_atomically(temp_dir: Path, stream_key: str, data: bytes) -> None:
    """Atomic tempfile+``os.replace`` write; intended to run on a worker thread."""
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / stream_key
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(temp_dir), prefix=f".{stream_key}.", suffix=".tmp")
    fp_taken = False
    try:
        with os.fdopen(tmp_fd, "wb") as fp:
            fp_taken = True
            fp.write(data)
        os.replace(tmp_path, target)
        tmp_path = None  # type: ignore[assignment]
    finally:
        if not fp_taken:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


class CDPFramePublisher:
    """Periodically publishes the active page's PNG to the streaming storage key.

    ``stream_key`` is the bare key the API-side WebSocket polls via
    ``StorageBase.get_streaming_file`` (e.g. ``"wr_123.png"``). CDP failures
    are tolerated; the loop continues. ``RealBrowserManager`` calls
    :meth:`start` after a working page exists and :meth:`stop` before closing
    the browser context.
    """

    def __init__(
        self,
        *,
        browser_state: BrowserState,
        stream_key: str,
        organization_id: str,
        capture_interval_seconds: float = DEFAULT_CAPTURE_INTERVAL_SECONDS,
    ) -> None:
        self._browser_state = browser_state
        self._stream_key = stream_key
        self._organization_id = organization_id
        self._capture_interval_seconds = max(capture_interval_seconds, 0.1)

        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._cdp_session: CDPSession | None = None
        self._attached_page: Page | None = None
        # Digest of the last frame whose write + upload both succeeded. Lets us
        # dedupe identical frames without losing a retry on transient upload
        # failure.
        self._last_published_digest: bytes | None = None

    @property
    def stream_key(self) -> str:
        return self._stream_key

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Spawn the background publish loop. Idempotent."""
        if self.is_running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name=f"cdp-frame-publisher:{self._stream_key}")
        LOG.info(
            "CDP frame publisher started",
            stream_key=self._stream_key,
            organization_id=self._organization_id,
            interval_seconds=self._capture_interval_seconds,
        )

    async def stop(self) -> None:
        """Cancel the loop, detach CDP session, and reset state. Idempotent."""
        self._stopped.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._detach_cdp_session()
        self._last_published_digest = None
        LOG.info(
            "CDP frame publisher stopped",
            stream_key=self._stream_key,
            organization_id=self._organization_id,
        )

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                # Self-terminate if the browser context is gone (persistent-session
                # handoff with ``close_browser_on_completion=False``, crash, or any
                # other path that drops the context without firing on-close).
                if not self._browser_state_is_connected():
                    LOG.info(
                        "CDP frame publisher self-terminating: browser context disconnected",
                        stream_key=self._stream_key,
                        organization_id=self._organization_id,
                    )
                    self._stopped.set()
                    break

                try:
                    await self._publish_one_frame()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.warning(
                        "CDP frame publish iteration failed",
                        stream_key=self._stream_key,
                        organization_id=self._organization_id,
                        exc_info=True,
                    )
                    await self._detach_cdp_session()

                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._capture_interval_seconds)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            return

    def _browser_state_is_connected(self) -> bool:
        """Cheap, never-raises wrapper around ``BrowserState.is_connected``.

        A stale ``BrowserState`` whose underlying Playwright driver has gone
        away will report ``False`` here; we use it as the loop's keep-going
        signal so the publisher does not spin forever after teardown.
        """
        try:
            return bool(self._browser_state.is_connected())
        except Exception:
            return False

    async def _publish_one_frame(self) -> None:
        page = await self._browser_state.get_working_page()
        if page is None:
            return

        if page is not self._attached_page or self._cdp_session is None:
            await self._detach_cdp_session()
            try:
                self._cdp_session = await page.context.new_cdp_session(page)
            except Exception:
                LOG.warning(
                    "Could not open CDP session for frame publishing",
                    stream_key=self._stream_key,
                    organization_id=self._organization_id,
                    exc_info=True,
                )
                self._cdp_session = None
                self._attached_page = None
                return
            self._attached_page = page
            self._last_published_digest = None
            LOG.info(
                "CDP frame publisher attached to page",
                stream_key=self._stream_key,
                organization_id=self._organization_id,
                page_url=getattr(page, "url", ""),
            )

        try:
            result = await self._cdp_session.send(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "captureBeyondViewport": False,
                },
            )
        except Exception:
            # Transient failure (target detached, navigation in progress, etc.).
            # Reattach on the next tick; logged at debug so a misbehaving remote
            # CDP doesn't flood the warning stream at 1 FPS.
            LOG.debug(
                "Page.captureScreenshot failed; will reattach next tick",
                stream_key=self._stream_key,
                organization_id=self._organization_id,
                exc_info=True,
            )
            await self._detach_cdp_session()
            return

        encoded = result.get("data", "") if isinstance(result, dict) else ""
        if not encoded:
            return
        try:
            data = base64.b64decode(encoded, validate=False)
        except (binascii.Error, ValueError):
            return
        if not data:
            return

        # Content-addressable dedupe hash (not a security boundary); SHA-256
        # to satisfy security scanners that block SHA-1.
        digest = hashlib.sha256(data).digest()
        if digest == self._last_published_digest:
            return

        write_ok = await self._write_frame(data)
        if write_ok:
            # Dedupe only after both local write and upload succeed, so a
            # transient upload failure retries instead of getting deduped away.
            self._last_published_digest = digest

    async def _write_frame(self, data: bytes) -> bool:
        """Persist one frame; True iff both the local write and the upload succeeded."""
        temp_dir = Path(get_skyvern_temp_dir()) / self._organization_id
        try:
            # Blocking I/O runs on a worker thread so a large flush does not
            # stall the event loop shared with other publishers / agent work.
            await asyncio.to_thread(_write_frame_atomically, temp_dir, self._stream_key, data)
        except OSError:
            LOG.warning(
                "Failed to write streaming frame to disk",
                stream_key=self._stream_key,
                organization_id=self._organization_id,
                exc_info=True,
            )
            return False

        # Local-disk storage reads the temp file directly; remote object-storage
        # backends need an explicit upload.
        try:
            await app.STORAGE.save_streaming_file(self._organization_id, self._stream_key)
        except Exception:
            LOG.debug(
                "save_streaming_file failed; will retry on next iteration",
                stream_key=self._stream_key,
                organization_id=self._organization_id,
                exc_info=True,
            )
            return False
        return True

    async def _detach_cdp_session(self) -> None:
        session = self._cdp_session
        self._cdp_session = None
        self._attached_page = None
        if session is None:
            return
        try:
            await session.detach()
        except Exception:
            pass


def stream_key_for_workflow_run(workflow_run_id: str) -> str:
    return f"{workflow_run_id}.png"


def stream_key_for_task(task_id: str) -> str:
    return f"{task_id}.png"
