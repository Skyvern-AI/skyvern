from __future__ import annotations

import asyncio
import errno
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import structlog

from skyvern.browser_extension.auth import load_or_create_pairing_token
from skyvern.browser_extension.errors import BrowserExtensionError
from skyvern.browser_extension.relay import ExtensionRelayServer
from skyvern.browser_extension.target_registry import VirtualTargetRegistry

LOG = structlog.get_logger(__name__)

_PORT_ENV = "SKYVERN_BROWSER_EXTENSION_PORT"
_DEFAULT_PORT = 19777


class _Adapter(Protocol):
    @property
    def cdp_ws_url(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def handle_extension_event(self, event: str, params: dict) -> None: ...

    async def on_extension_disconnect(self) -> None: ...


_relay_factory: Callable[
    [str, int, Callable[[str, dict], Awaitable[None]], Callable[[], Awaitable[None]] | None],
    ExtensionRelayServer,
] = ExtensionRelayServer
_adapter_factory: Callable[[VirtualTargetRegistry, ExtensionRelayServer], _Adapter] | None = None


class BrowserExtensionRuntime:
    _instance: BrowserExtensionRuntime | None = None
    _lock = asyncio.Lock()

    def __init__(self, relay: ExtensionRelayServer, adapter: _Adapter) -> None:
        self._relay = relay
        self._adapter = adapter
        self._stopped = False

    @classmethod
    async def get_or_start(cls, port: int | None = None) -> BrowserExtensionRuntime:
        async with cls._lock:
            if cls._instance is not None:
                return cls._instance

            resolved_port = _resolve_port(port)
            token = load_or_create_pairing_token()
            registry = VirtualTargetRegistry()
            adapter_holder: list[_Adapter] = []

            async def handle_event(event: str, params: dict) -> None:
                await adapter_holder[0].handle_extension_event(event, params)

            async def on_disconnect() -> None:
                await adapter_holder[0].on_extension_disconnect()

            relay = _relay_factory(token, resolved_port, handle_event, on_disconnect)
            adapter = _create_adapter(registry, relay)
            adapter_holder.append(adapter)

            try:
                await adapter.start()
                await relay.start()
            except OSError as exc:
                await _cleanup_failed_start(relay, adapter)
                if exc.errno == errno.EADDRINUSE:
                    raise BrowserExtensionError(
                        f"Another Skyvern MCP session likely owns the browser-extension bridge on port {resolved_port}; "
                        f"close that session, or set {_PORT_ENV} to a free port and update the bridge port in the Skyvern "
                        "extension popup to match"
                    ) from exc
                raise
            except BaseException:
                await _cleanup_failed_start(relay, adapter)
                raise

            runtime = cls(relay, adapter)
            cls._instance = runtime
            return runtime

    @classmethod
    def instance(cls) -> BrowserExtensionRuntime | None:
        return cls._instance

    @property
    def cdp_ws_url(self) -> str:
        return self._adapter.cdp_ws_url

    @property
    def extension_connected(self) -> bool:
        return self._relay.connected

    async def wait_for_extension(self, timeout: float = 10.0) -> bool:
        return await self._relay.wait_connected(timeout)

    async def shutdown(self) -> None:
        cls = type(self)
        async with cls._lock:
            if self._stopped:
                return
            self._stopped = True
            try:
                await self._relay.stop()
            finally:
                try:
                    await self._adapter.stop()
                finally:
                    if cls._instance is self:
                        cls._instance = None

    @staticmethod
    def extension_dir() -> Path:
        return Path(__file__).resolve().parent / "extension"

    @staticmethod
    def configured_port() -> int:
        return _resolve_port(None)


def _create_adapter(registry: VirtualTargetRegistry, relay: ExtensionRelayServer) -> _Adapter:
    if _adapter_factory is not None:
        return _adapter_factory(registry, relay)
    from skyvern.browser_extension.cdp_adapter import ExtensionCdpAdapter

    return ExtensionCdpAdapter(registry, relay)


def _resolve_port(port: int | None) -> int:
    if port is not None:
        resolved_port = port
    else:
        environment_port = os.environ.get(_PORT_ENV)
        if environment_port is None:
            resolved_port = _DEFAULT_PORT
        else:
            try:
                resolved_port = int(environment_port)
            except ValueError as exc:
                raise BrowserExtensionError(f"{_PORT_ENV} must be an integer port") from exc
    if type(resolved_port) is not int or not 1 <= resolved_port <= 65535:
        raise BrowserExtensionError(f"{_PORT_ENV} must be a port between 1 and 65535")
    return resolved_port


async def _cleanup_failed_start(relay: ExtensionRelayServer, adapter: _Adapter) -> None:
    try:
        await relay.stop()
    except Exception:
        LOG.exception("failed to stop browser extension relay after startup error")
    try:
        await adapter.stop()
    except Exception:
        LOG.exception("failed to stop browser extension adapter after startup error")
