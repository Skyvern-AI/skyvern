"""A page whose upload button creates its file input only when clicked, so only a chooser listener sees it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from skyvern.forge.sdk.workflow.code_block_authorized_files import FileChooserHandle

ChooserListener = Callable[[FileChooserHandle], None]


async def _drive(timeout: float | None) -> None:
    if timeout is None:
        await asyncio.Event().wait()
    else:
        await asyncio.sleep(timeout / 1000)


async def _outlive_cancel(page: FakeChooserPage, timeout: float | None) -> None:
    # Like Playwright's driver: cancelling the caller leaves the call running until its own timeout.
    driver_call = asyncio.ensure_future(_drive(timeout))
    page.driver_calls.append(driver_call)
    await asyncio.shield(driver_call)


class FakeChooserHandle:
    def __init__(self, page: FakeChooserPage, into: list[dict[str, str | bytes]] | None = None) -> None:
        self._page = page
        self._into = page.chosen if into is None else into

    async def set_files(self, files: dict[str, str | bytes], *, timeout: float | None = None) -> None:
        self._page.set_files_timeouts.append(timeout)
        if self._page.set_files_outlives_cancel:
            await _outlive_cancel(self._page, timeout)
        if self._page.set_files_error is not None:
            raise self._page.set_files_error
        self._into.append(files)


class FakeChooserTrigger:
    def __init__(self, page: FakeChooserPage, selector: str) -> None:
        self.page, self.selector = page, selector

    async def click(self, *, timeout: float | None = None, trial: bool | None = None) -> None:
        page = self.page
        page.clicks.append((self.selector, bool(trial)))
        page.click_timeouts.append(timeout)
        if trial:
            if page.trial_error is not None:
                raise page.trial_error
            if page.trial_opens_chooser:
                for handler in list(page.listeners.get("filechooser", [])):
                    handler(FakeChooserHandle(page, into=page.stray_chosen))
            return
        if page.click_blocks:
            await asyncio.Event().wait()
        if page.click_outlives_cancel:
            await _outlive_cancel(page, timeout)
        if page.click_error is not None:
            raise page.click_error
        if page.opens_chooser:
            for handler in list(page.listeners.get("filechooser", [])):
                handler(FakeChooserHandle(page))

    async def set_input_files(self, files: dict[str, str | bytes]) -> None:
        self.page.input_files.append(files)


class FakeChooserPage:
    def __init__(
        self,
        *,
        opens_chooser: bool = True,
        trial_error: Exception | None = None,
        click_error: Exception | None = None,
        set_files_error: Exception | None = None,
        click_blocks: bool = False,
        click_outlives_cancel: bool = False,
        set_files_outlives_cancel: bool = False,
        trial_opens_chooser: bool = False,
    ) -> None:
        self.opens_chooser = opens_chooser
        self.trial_error = trial_error
        self.click_error = click_error
        self.set_files_error = set_files_error
        self.click_blocks = click_blocks
        self.click_outlives_cancel = click_outlives_cancel
        self.set_files_outlives_cancel = set_files_outlives_cancel
        self.trial_opens_chooser = trial_opens_chooser
        self.clicks: list[tuple[str, bool]] = []
        self.click_timeouts: list[float | None] = []
        self.set_files_timeouts: list[float | None] = []
        self.driver_calls: list[asyncio.Future[None]] = []
        self.chosen: list[dict[str, str | bytes]] = []
        self.stray_chosen: list[dict[str, str | bytes]] = []
        self.input_files: list[dict[str, str | bytes]] = []
        self.listeners: dict[str, list[ChooserListener]] = {}

    def locator(self, selector: str) -> FakeChooserTrigger:
        return FakeChooserTrigger(self, selector)

    def on(self, event: str, handler: ChooserListener) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: ChooserListener) -> None:
        self.listeners[event].remove(handler)

    @property
    def dispatched_clicks(self) -> int:
        return sum(1 for _, trial in self.clicks if not trial)

    @property
    def live_listeners(self) -> int:
        return len(self.listeners.get("filechooser", []))


# Chooser mode is offered by page class family, so the fakes claim the module of the page they stand in for.
FakeChooserPage.__module__ = "playwright.async_api._generated"


class FakeRawCDPChooserPage(FakeChooserPage):
    pass


FakeRawCDPChooserPage.__module__ = "skyvern.webeye.skycdp.facade.page"
