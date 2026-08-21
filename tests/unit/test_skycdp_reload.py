from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from skyvern.webeye.skycdp.errors import CdpTimeoutError
from skyvern.webeye.skycdp.facade.page import Page


class _ReloadSession:
    def __init__(self) -> None:
        self.detached = False
        self.reload_sent = asyncio.Event()
        self.handlers: dict[str, list[Callable[[dict], None]]] = {}

    async def send(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        if method == "Page.reload":
            assert timeout is not None and timeout > 0
            self.reload_sent.set()
        return {}

    def on(self, event: str, handler: Callable[[dict], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Callable[[dict], None]) -> None:
        self.handlers[event].remove(handler)

    def emit(self, event: str, params: dict) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(params)


@pytest.mark.asyncio
async def test_reload_waits_for_a_new_main_frame_commit_and_load_event() -> None:
    session = _ReloadSession()
    page = Page(object(), session)  # type: ignore[arg-type]
    page._main_frame_id = "main"
    page._bind_session_events(session)

    reloading = asyncio.create_task(page.reload(timeout=5_000, wait_until="domcontentloaded"))
    await session.reload_sent.wait()

    assert not reloading.done()
    session.emit("Page.frameNavigated", {"frame": {"id": "subframe", "url": "https://frame.example"}})
    assert not reloading.done()
    session.emit("Page.loadEventFired", {})
    assert not reloading.done()

    session.emit("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.test"}})
    assert not reloading.done()
    session.emit("Page.loadEventFired", {})
    await reloading

    assert not page._listeners.get("framenavigated")
    assert session.handlers["Page.loadEventFired"] == []


@pytest.mark.asyncio
async def test_reload_times_out_when_load_event_never_arrives() -> None:
    session = _ReloadSession()
    page = Page(object(), session)  # type: ignore[arg-type]
    page._main_frame_id = "main"
    page._bind_session_events(session)

    reloading = asyncio.create_task(page.reload(timeout=50, wait_until="load"))
    await session.reload_sent.wait()
    session.emit("Page.frameNavigated", {"frame": {"id": "main", "url": "https://example.test"}})

    with pytest.raises(CdpTimeoutError, match="reload did not commit and load a new main-frame document"):
        await reloading

    assert not page._listeners.get("framenavigated")
    assert session.handlers["Page.loadEventFired"] == []
