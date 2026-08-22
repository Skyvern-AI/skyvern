from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from playwright._impl._connection import Connection
from playwright._impl._errors import TargetClosedError
from playwright._impl._transport import Transport

from skyvern.webeye.driver_connection import close_driver_connection_on_transport_loss


class _RecordingTransport(Transport):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(loop)
        self.sent: list[dict] = []

    def request_stop(self) -> None:
        return None

    async def wait_until_stopped(self) -> None:
        return None

    async def connect(self) -> None:
        return None

    async def run(self) -> None:
        return None

    def send(self, message: dict) -> None:
        self.sent.append(message)


def _connection_with(transport: Transport, loop: asyncio.AbstractEventLoop) -> Connection:
    connection = Connection(None, lambda *args: None, transport, loop)
    connection._api_zone.set(cast(Any, {"apiName": "test", "frames": [], "title": None}))
    return connection


@pytest.mark.asyncio
async def test_a_lost_driver_transport_stops_further_writes_and_unblocks_pending_calls() -> None:
    # SKY-14645: Playwright sets `_closed_error` only from an explicit stop(), so a driver that dies
    # on its own leaves every retained handle writing into the closed pipe (asyncio warns once per
    # write) while its reply never arrives.
    loop = asyncio.get_running_loop()
    transport = _RecordingTransport(loop)
    connection = _connection_with(transport, loop)
    owner = cast(Any, SimpleNamespace(_guid="root", _was_collected=False))

    close_driver_connection_on_transport_loss(SimpleNamespace(_connection=connection))

    in_flight = connection._send_message_to_server(owner, "Browser.newContext", {})
    assert len(transport.sent) == 1

    transport.on_error_future.set_exception(Exception("Connection closed while reading from the driver"))
    await asyncio.sleep(0)

    assert isinstance(in_flight.future.exception(), TargetClosedError)
    with pytest.raises(TargetClosedError):
        connection._send_message_to_server(owner, "Browser.newContext", {})
    assert len(transport.sent) == 1


@pytest.mark.asyncio
async def test_arming_a_driver_without_playwright_internals_is_a_no_op() -> None:
    # An attach-only engine is a driver by duck type only; it exposes no connection/transport to arm.
    close_driver_connection_on_transport_loss(SimpleNamespace())
    close_driver_connection_on_transport_loss(SimpleNamespace(_connection=SimpleNamespace()))
