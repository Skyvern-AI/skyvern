from __future__ import annotations

import asyncio
import os

import aiofiles
import structlog
from playwright.async_api import Page
from pydantic import BaseModel, PrivateAttr

LOG = structlog.get_logger()


class VideoArtifact(BaseModel):
    video_path: str | None = None
    video_artifact_id: str | None = None
    video_file_extension: str | None = None
    video_data: bytes = b""


class BrowserArtifacts(BaseModel):
    video_artifacts: list[VideoArtifact] = []
    har_path: str | None = None
    traces_dir: str | None = None
    browser_session_dir: str | None = None
    browser_console_log_path: str | None = None
    # Set by remote-CDP creators so RealBrowserManager attaches a CDP frame
    # publisher. Local Playwright contexts leave it False. Type must stay
    # ``bool`` — the manager guards with ``is True`` identity, not truthiness.
    needs_cdp_frame_publisher: bool = False
    # Optional opaque identifier for a remote browser session.
    remote_browser_session_id: str | None = None
    _browser_console_log_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    # Tombstoned synchronously before any await, so set_popup_video_listener can't
    # re-register a page's video after RealBrowserState decides to discard it.
    _discarded_pages: set[Page] = PrivateAttr(default_factory=set)
    # Freshness-guard state, captured at seed and read at write-back (in-memory only, per browser run).
    # _seed_cookie_snapshot / _seed_profile_etag record what the profile held when this run seeded it;
    # _run_performed_fresh_login flips true once a login block types a fresh sign-in.
    _seed_cookie_snapshot: list[dict] | None = PrivateAttr(default=None)
    _seed_profile_etag: str | None = PrivateAttr(default=None)
    _run_performed_fresh_login: bool = PrivateAttr(default=False)
    # _seed_load_failed flips true when a saved profile failed to launch (corruption/stale lock) and the
    # run fell back to a blank dir — its end-state is not this profile's, so write-back must be suppressed.
    _seed_load_failed: bool = PrivateAttr(default=False)
    # _seed_capture_failed flips true when the seed fingerprint could not be captured — the guard then
    # treats the seed as UNKNOWN and never full-overwrites (a None etag would otherwise read "unchanged").
    _seed_capture_failed: bool = PrivateAttr(default=False)

    def record_seed_profile_state(self, cookies: list[dict], etag: str | None) -> None:
        self._seed_cookie_snapshot = cookies
        self._seed_profile_etag = etag

    def mark_run_performed_fresh_login(self) -> None:
        self._run_performed_fresh_login = True

    def mark_seed_load_failed(self) -> None:
        self._seed_load_failed = True

    def mark_seed_capture_failed(self) -> None:
        self._seed_capture_failed = True

    def discard_page_video(self, page: Page) -> None:
        self._discarded_pages.add(page)

    def is_page_video_discarded(self, page: Page) -> bool:
        return page in self._discarded_pages

    async def append_browser_console_log(self, msg: str) -> int:
        if self.browser_console_log_path is None:
            return 0

        async with self._browser_console_log_lock:
            async with aiofiles.open(self.browser_console_log_path, "a") as f:
                return await f.write(msg)

    async def _read_console_log_file(self) -> bytes:
        if self.browser_console_log_path is None:
            return b""

        if not os.path.exists(self.browser_console_log_path):
            return b""
        async with aiofiles.open(self.browser_console_log_path, "rb") as f:
            return await f.read()

    async def read_browser_console_log(self, timeout: float = 5) -> bytes:
        if self.browser_console_log_path is None:
            return b""

        try:
            async with asyncio.timeout(timeout):
                async with self._browser_console_log_lock:
                    return await self._read_console_log_file()
        except asyncio.TimeoutError:
            LOG.warning(
                "Failed to acquire browser console log lock, reading file without lock (may be incomplete)",
                timeout=timeout,
            )
            return await self._read_console_log_file()
