from __future__ import annotations

from pathlib import Path

from playwright.async_api import BrowserContext, Download

DOWNLOAD_FAILURE_READ_TIMEOUT_SECONDS = 5.0


class NativeDownloadAttempt:
    def __init__(self, observer: NativeDownloads, task_id: str) -> None:
        self.observer = observer
        self.task_id = task_id
        self.pending: set[Download] = set()
        self.interrupted = 0
        self.cancelled_by_skyvern = 0
        self.closed = False

    def started(self, download: Download) -> None:
        if not self.closed:
            self.pending.add(download)

    def finished(self, download: Download, *, failed: bool = False, self_cancelled: bool = False) -> None:
        if self.closed or download not in self.pending:
            return
        self.pending.remove(download)
        if self_cancelled:
            self.cancelled_by_skyvern += 1
        elif failed:
            self.interrupted += 1

    def end(self) -> None:
        self.closed = True
        self.pending.clear()

    async def failed_and_quiescent(self) -> bool:
        if self.closed or self.pending or not (self.interrupted or self.cancelled_by_skyvern):
            return False
        return (
            await self.observer.quiescent() and not self.closed and not self.pending and self.observer.attempt is self
        )


class NativeDownloads:
    """Context-wide native download liveness from the Playwright Download handles the page listeners receive.

    Playwright reports downloads from no-opener tabs on the opener page, so these handles cover the context only when
    the listeners were registered before any page activity; otherwise the context stays uncovered.
    """

    def __init__(self, context: BrowserContext, download_dir: str | None, *, covered: bool) -> None:
        self.context = context
        self.download_dir = Path(download_dir).resolve() if download_dir is not None else None
        self._covered = covered
        self._pending: set[Download] = set()
        self.attempt: NativeDownloadAttempt | None = None
        context.on("close", self.invalidate)

    @classmethod
    def attach(
        cls, context: BrowserContext, *, before_activity: bool, download_dir: str | None = None
    ) -> NativeDownloads:
        return cls(context, download_dir, covered=before_activity)

    def invalidate(self, *_: object) -> None:
        self._covered = False
        if self.attempt is not None:
            self.attempt.end()

    def listeners_registered(self) -> None:
        if any(page.url != "about:blank" for page in self.context.pages):
            self.invalidate()

    def started(self, download: Download) -> NativeDownloadAttempt | None:
        self._pending.add(download)
        attempt = self.attempt
        if attempt is not None:
            attempt.started(download)
        return attempt

    def finished(
        self,
        download: Download,
        attempt: NativeDownloadAttempt | None,
        *,
        failed: bool = False,
        self_cancelled: bool = False,
    ) -> None:
        self._pending.discard(download)
        if attempt is not None:
            attempt.finished(download, failed=failed, self_cancelled=self_cancelled)

    def begin_attempt(self, task_id: str) -> NativeDownloadAttempt:
        if self.attempt is not None:
            self.attempt.end()
        self.attempt = NativeDownloadAttempt(self, task_id)
        return self.attempt

    async def quiescent(self) -> bool:
        return self._covered and not self._pending
