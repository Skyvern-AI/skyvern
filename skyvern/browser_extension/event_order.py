from __future__ import annotations

import asyncio


class EventHold:
    def __init__(self, tab_id: int) -> None:
        self.tab_id = tab_id
        self.activated = False
        self.trigger_read_number: int | None = None
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def activate(self) -> None:
        if not self.future.done():
            self.activated = True

    def release(self) -> None:
        if not self.future.done():
            self.future.set_result(None)
