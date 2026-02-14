from __future__ import annotations

import asyncio
import os

import aiofiles
import structlog
from pydantic import BaseModel, PrivateAttr

LOG = structlog.get_logger()


class VideoArtifact(BaseModel):
    video_path: str | None = None
    video_artifact_id: str | None = None
    video_data: bytes = b""


class BrowserArtifacts(BaseModel):
    video_artifacts: list[VideoArtifact] = []
    har_path: str | None = None
    traces_dir: str | None = None
    browser_session_dir: str | None = None
    browser_console_log_path: str | None = None
    _browser_console_log_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

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
