"""Downloads, file choosers and console messages.

These three share a shape: production registers a listener for each and, until now, got a listener
that never fired. That is worse than an unimplemented method -- an AttributeError stops the run and
names itself, while a dead listener lets the run continue and quietly lose the download, the upload,
or the console log the exfiltration channel depends on.

Downloads are browser-level in CDP: ``Browser.downloadWillBegin`` and ``Browser.downloadProgress``
arrive with no session id, so they are subscribed once on the connection and routed to a page by the
frame id they carry. Chrome names the artifact by GUID rather than by filename, which is why the
suggested name is carried separately and why the existing listener re-adds the extension.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.webeye.skycdp.errors import CdpError

if TYPE_CHECKING:
    from skyvern.webeye.skycdp.connection import CdpConnection, CdpSession

LOG = structlog.get_logger()


class Download:
    """One download, resolved as Chrome reports progress.

    ``path()`` and ``failure()`` both wait for the transfer to finish, because a caller asking either
    question wants the settled answer; Playwright behaves the same way.
    """

    def __init__(self, connection: CdpConnection, guid: str, url: str, suggested_filename: str, page: Any) -> None:
        self._connection = connection
        self._guid = guid
        self.page = page
        self._url = url
        self._suggested_filename = suggested_filename
        self._finished: asyncio.Future[str | None] = asyncio.get_event_loop().create_future()
        self._failure: str | None = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def suggested_filename(self) -> str:
        return self._suggested_filename

    def note_progress(self, params: dict[str, Any]) -> None:
        state = params.get("state")
        if state == "completed":
            if not self._finished.done():
                self._finished.set_result(params.get("filePath"))
        elif state == "canceled":
            self._failure = "canceled"
            if not self._finished.done():
                self._finished.set_result(None)

    async def path(self) -> str | None:
        try:
            return await asyncio.wait_for(asyncio.shield(self._finished), timeout=120)
        except asyncio.TimeoutError:
            return None

    async def failure(self) -> str | None:
        await self.path()
        return self._failure

    async def save_as(self, destination: str | Path) -> None:
        source = await self.path()
        if source is None:
            raise CdpError(f"download {self._guid} produced no file ({self._failure or 'still in progress'})")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    async def delete(self) -> None:
        source = await self.path()
        if source:
            Path(source).unlink(missing_ok=True)

    async def cancel(self) -> None:
        with_guid = {"guid": self._guid}
        try:
            await self._connection.transport.send("Browser.cancelDownload", with_guid)
        except CdpError:
            LOG.debug("skycdp could not cancel a download", exc_info=True)


class FileChooser:
    """An intercepted file-input dialog.

    Interception is enabled for every page whether or not anyone listens, because that is what stops
    a native dialog appearing and blocking a headless run at a modal nothing can dismiss.
    """

    def __init__(self, session: CdpSession, page: Any, backend_node_id: int, multiple: bool) -> None:
        self._session = session
        self.page = page
        self._backend_node_id = backend_node_id
        self._multiple = multiple

    def is_multiple(self) -> bool:
        """A method, not a property -- Playwright's is `is_multiple()`.

        A property here would return the bound method to `if chooser.is_multiple()`, which is truthy
        for both answers, so a single-file input would read as multi-file and never fail visibly.
        """
        return self._multiple

    async def set_files(self, files: str | list[str]) -> None:
        paths = [files] if isinstance(files, str) else list(files)
        missing = [path for path in paths if not Path(path).is_file()]
        if missing:
            raise CdpError(f"cannot upload files that do not exist: {missing}")
        await self._session.send("DOM.setFileInputFiles", {"files": paths, "backendNodeId": self._backend_node_id})


class ConsoleMessage:
    """A console entry, as `page.on("console")` delivers it."""

    def __init__(self, params: dict[str, Any]) -> None:
        self._type = str(params.get("type", "log"))
        self._args = list(params.get("args") or [])
        stack = (params.get("stackTrace") or {}).get("callFrames") or [{}]
        top = stack[0] if stack else {}
        self._location = {
            "url": str(top.get("url", "")),
            "lineNumber": int(top.get("lineNumber", 0)),
            "columnNumber": int(top.get("columnNumber", 0)),
        }

    @property
    def type(self) -> str:
        return self._type

    @property
    def location(self) -> dict[str, Any]:
        return dict(self._location)

    @property
    def args(self) -> list[Any]:
        return list(self._args)

    @property
    def text(self) -> str:
        """The rendered message.

        A property, not a method -- Playwright's is a property and production reads `msg.text`
        without parentheses (`browser_factory.py`). As a method it would log the repr of a bound
        method on every console line and never raise.

        Built from the by-value previews Chrome already sent rather than by resolving each argument:
        resolving would mean a round trip per console line, and console output is exactly the
        high-volume path where that cost would be felt.
        """
        rendered = []
        for argument in self._args:
            if "value" in argument:
                rendered.append(str(argument["value"]))
            elif argument.get("description"):
                rendered.append(str(argument["description"]))
            else:
                rendered.append(str(argument.get("type", "")))
        return " ".join(rendered)

    def __str__(self) -> str:
        return self.text
