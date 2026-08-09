from __future__ import annotations

from typing import Protocol


class ExtensionTransport(Protocol):
    """What the CDP adapter and the runtime need from whatever owns the extension socket.

    Satisfied both by ExtensionRelayServer, which owns the socket in-process, and by
    BrokerTransport, which reaches it through the shared broker daemon.
    """

    bound_port: int
    scoped_tabs: list[dict]

    @property
    def connected(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def wait_connected(self, timeout: float) -> bool: ...

    async def request(self, op: str, args: dict, timeout: float = 30.0) -> dict: ...

    async def acquire_pairing_nonce(self) -> str: ...
