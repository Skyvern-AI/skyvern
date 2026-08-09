from __future__ import annotations

import asyncio
import errno
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import structlog

from skyvern.browser_extension.auth import load_or_create_pairing_token
from skyvern.browser_extension.broker.client import BrokerTransport, LegacyBridgeOwnerError
from skyvern.browser_extension.errors import BrowserExtensionError
from skyvern.browser_extension.relay import ExtensionRelayServer
from skyvern.browser_extension.target_registry import VirtualTargetRegistry
from skyvern.browser_extension.transport import ExtensionTransport

LOG = structlog.get_logger(__name__)

_PORT_ENV = "SKYVERN_BROWSER_EXTENSION_PORT"
_BROKER_ENV = "SKYVERN_BROWSER_EXTENSION_BROKER"
_DEFAULT_PORT = 19777
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


class _Adapter(Protocol):
    @property
    def cdp_ws_url(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def handle_extension_event(self, event: str, params: dict) -> None: ...

    async def on_extension_disconnect(self) -> None: ...


TransportFactory = Callable[
    [str, int, Callable[[str, dict], Awaitable[None]], Callable[[], Awaitable[None]] | None],
    ExtensionTransport,
]

_relay_factory: TransportFactory = ExtensionRelayServer
_broker_factory: TransportFactory = BrokerTransport
_adapter_factory: Callable[[VirtualTargetRegistry, ExtensionTransport], _Adapter] | None = None


def broker_enabled() -> bool:
    return os.environ.get(_BROKER_ENV, "").strip().lower() not in _DISABLED_VALUES


def _open_browser_process(command: list[str], *, windows: bool) -> bool:
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    options["creationflags" if windows else "start_new_session"] = 0x00000208 if windows else True
    try:
        subprocess.Popen(command, **options)
    except OSError:
        return False
    return True


def _open_browser_process(command: list[str], *, windows: bool) -> bool:
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    options["creationflags" if windows else "start_new_session"] = 0x00000208 if windows else True
    try:
        subprocess.Popen(command, **options)
    except OSError:
        return False
    return True


class BrowserExtensionRuntime:
    _instance: BrowserExtensionRuntime | None = None
    _lock = asyncio.Lock()

    def __init__(self, transport: ExtensionTransport, adapter: _Adapter, *, brokered: bool) -> None:
        self._transport = transport
        self._adapter = adapter
        self._brokered = brokered
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

            brokered = broker_enabled()
            factory = _broker_factory if brokered else _relay_factory
            transport = factory(token, resolved_port, handle_event, on_disconnect)
            adapter = _create_adapter(registry, transport)
            adapter_holder.append(adapter)

            try:
                await adapter.start()
                await transport.start()
            except LegacyBridgeOwnerError as exc:
                await _cleanup_failed_start(transport, adapter)
                raise BrowserExtensionError(
                    f"An older Skyvern MCP session owns the browser-extension bridge on port {resolved_port} and "
                    "cannot share it; restart that session on this version so every agent can attach, or set "
                    f"{_PORT_ENV} to a free port and update the bridge port in the Skyvern extension popup to match"
                ) from exc
            except OSError as exc:
                await _cleanup_failed_start(transport, adapter)
                if exc.errno == errno.EADDRINUSE:
                    raise BrowserExtensionError(
                        f"Another Skyvern MCP session likely owns the browser-extension bridge on port {resolved_port}; "
                        f"close that session, or set {_PORT_ENV} to a free port and update the bridge port in the Skyvern "
                        "extension popup to match"
                    ) from exc
                raise
            except BaseException:
                await _cleanup_failed_start(transport, adapter)
                raise

            runtime = cls(transport, adapter, brokered=brokered)
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
        return self._transport.connected

    @property
    def brokered(self) -> bool:
        return self._brokered

    async def wait_for_extension(self, timeout: float = 10.0) -> bool:
        return await self._transport.wait_connected(timeout)

    @staticmethod
    def open_extension_url(url: str) -> bool:
        if sys.platform == "darwin":
            executable = shutil.which("open")
            if executable is None:
                return False
            for app_name in ("Google Chrome", "Google Chrome Beta", "Google Chrome Dev", "Google Chrome Canary"):
                try:
                    subprocess.run(
                        [executable, "-a", app_name, url],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except (OSError, subprocess.CalledProcessError):
                    continue
                return True
            return False

        if sys.platform.startswith("linux"):
            for browser_name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
                executable = shutil.which(browser_name)
                if executable is None:
                    continue
                if _open_browser_process([executable, url], windows=False):
                    return True
            return False

        if sys.platform == "win32":
            roots = (
                os.environ.get("LOCALAPPDATA"),
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
            )
            for root in roots:
                if not root:
                    continue
                executable = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if not executable.is_file():
                    continue
                if _open_browser_process([str(executable), url], windows=True):
                    return True
        return False

    async def open_pairing_page(self) -> bool:
        try:
            nonce = await self._transport.acquire_pairing_nonce()
        except BrowserExtensionError as exc:
            LOG.info("browser_extension_pairing_page_unavailable", error_type=type(exc).__name__)
            return False
        url = f"http://127.0.0.1:{self._transport.bound_port}/pair#{nonce}"
        return self.open_extension_url(url)

    @staticmethod
    def open_extension_url(url: str) -> bool:
        if sys.platform == "darwin":
            executable = shutil.which("open")
            if executable is None:
                return False
            for app_name in ("Google Chrome", "Google Chrome Beta", "Google Chrome Dev", "Google Chrome Canary"):
                try:
                    subprocess.run(
                        [executable, "-a", app_name, url],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except (OSError, subprocess.CalledProcessError):
                    continue
                return True
            return False

        if sys.platform.startswith("linux"):
            for browser_name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
                executable = shutil.which(browser_name)
                if executable is None:
                    continue
                if _open_browser_process([executable, url], windows=False):
                    return True
            return False

        if sys.platform == "win32":
            roots = (
                os.environ.get("LOCALAPPDATA"),
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
            )
            for root in roots:
                if not root:
                    continue
                executable = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if not executable.is_file():
                    continue
                if _open_browser_process([str(executable), url], windows=True):
                    return True
        return False

    def open_pairing_page(self) -> bool:
        nonce = self._relay.get_or_create_pairing_nonce()
        url = f"http://127.0.0.1:{self._relay.bound_port}/pair#{nonce}"
        return self.open_extension_url(url)

    async def shutdown(self) -> None:
        cls = type(self)
        async with cls._lock:
            if self._stopped:
                return
            self._stopped = True
            try:
                await self._transport.stop()
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


def _create_adapter(registry: VirtualTargetRegistry, transport: ExtensionTransport) -> _Adapter:
    if _adapter_factory is not None:
        return _adapter_factory(registry, transport)
    from skyvern.browser_extension.cdp_adapter import ExtensionCdpAdapter

    return ExtensionCdpAdapter(registry, transport)


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


async def _cleanup_failed_start(transport: ExtensionTransport, adapter: _Adapter) -> None:
    try:
        await transport.stop()
    except Exception:
        LOG.exception("failed to stop browser extension relay after startup error")
    try:
        await adapter.stop()
    except Exception:
        LOG.exception("failed to stop browser extension adapter after startup error")
