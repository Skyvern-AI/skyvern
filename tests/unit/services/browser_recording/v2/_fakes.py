from collections import defaultdict
from collections.abc import Callable
from typing import Any


class FakeCdpSession:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.listeners_at_send: list[set[str]] = []
        self.listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = defaultdict(list)

    async def send(self, method: str, params: dict[str, Any]) -> None:
        self.sent.append((method, params))
        self.listeners_at_send.append(set(self.listeners))

    def on(self, event: str, callback: Callable[[dict[str, Any]], None]) -> None:
        self.listeners[event].append(callback)

    def remove_listener(self, event: str, callback: Callable[[dict[str, Any]], None]) -> None:
        self.listeners[event].remove(callback)

    def fire(self, event: str, payload: dict[str, Any]) -> None:
        for callback in list(self.listeners[event]):
            callback(payload)
