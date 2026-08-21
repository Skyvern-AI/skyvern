from __future__ import annotations

import asyncio
import errno
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

import structlog

from skyvern.browser_extension.auth import load_or_create_pairing_token
from skyvern.browser_extension.broker_client import BrokerClient
from skyvern.browser_extension.errors import BrowserExtensionError
from skyvern.browser_extension.relay import ExtensionRelayServer
from skyvern.browser_extension.target_registry import VirtualTargetRegistry

LOG = structlog.get_logger(__name__)

_PORT_ENV = "SKYVERN_BROWSER_EXTENSION_PORT"
_BROKER_ENV = "SKYVERN_BROWSER_EXTENSION_BROKER"
_DEFAULT_PORT = 19777
_PAIRING_BUSY_WAIT_SECONDS = 30.0


class _Adapter(Protocol):
    @property
    def cdp_ws_url(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def handle_extension_event(self, event: str, params: dict) -> None: ...

    async def on_extension_disconnect(self) -> None: ...


class _Relay(Protocol):
    bound_port: int
    scoped_tabs: list[dict]

    @property
    def connected(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def wait_connected(self, timeout: float) -> bool: ...

    async def request(self, op: str, args: dict, timeout: float = 30.0) -> dict: ...

    async def ensure_root_lease(self) -> dict | None: ...

    async def release_tab(self, tab_id: int) -> None: ...


_relay_factory: Callable[
    [str, int, Callable[[str, dict], Awaitable[None]], Callable[[], Awaitable[None]] | None],
    ExtensionRelayServer,
] = ExtensionRelayServer
_adapter_factory: Callable[[VirtualTargetRegistry, _Relay], _Adapter] | None = None


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

    def __init__(self, relay: _Relay, adapter: _Adapter) -> None:
        self._relay = relay
        self._adapter = adapter
        self._stopped = False

    @classmethod
    async def get_or_start(cls, port: int | None = None) -> BrowserExtensionRuntime:
        async with cls._lock:
            if cls._instance is not None:
                return cls._instance

            resolved_port = _resolve_port(port)
            if _broker_requested() and not _broker_platform_supported():
                LOG.info(
                    "browser_extension_broker_unsupported_platform_using_legacy",
                    code="UNSUPPORTED_PLATFORM",
                    platform=sys.platform,
                )
            if broker_mode_enabled():
                registry = VirtualTargetRegistry()
                broker_adapter_holder: list[_Adapter] = []

                async def handle_broker_event(event: str, params: dict) -> None:
                    await broker_adapter_holder[0].handle_extension_event(event, params)

                async def on_broker_disconnect() -> None:
                    await broker_adapter_holder[0].on_extension_disconnect()

                broker = BrokerClient(resolved_port, handle_broker_event, on_broker_disconnect)
                broker_adapter = _create_adapter(registry, broker)
                broker_adapter_holder.append(broker_adapter)
                try:
                    await broker_adapter.start()
                    await broker.start()
                except BaseException:
                    await _cleanup_failed_start(broker, broker_adapter)
                    raise

                runtime = cls(broker, broker_adapter)
                cls._instance = runtime
                return runtime

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
        if isinstance(self._relay, BrokerClient):
            return False
        embedded_relay = cast(ExtensionRelayServer, self._relay)
        nonce = embedded_relay.get_or_create_pairing_nonce()
        url = f"http://127.0.0.1:{embedded_relay.bound_port}/pair#{nonce}"
        return self.open_extension_url(url)

    async def begin_pairing(self) -> bool:
        """Open this client's one-click pairing page after any current approval completes."""
        if not isinstance(self._relay, BrokerClient):
            return self.open_pairing_page()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PAIRING_BUSY_WAIT_SECONDS
        while True:
            try:
                result = await self._relay.begin_pairing()
            except BrowserExtensionError as exc:
                error_code = getattr(exc, "code", None)
                if error_code == "EXTENSION_UPGRADE_REQUIRED":
                    raise
                if error_code != "PAIRING_BUSY":
                    LOG.debug("browser_extension_pairing_begin_failed", error_type=type(exc).__name__)
                    return False
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                try:
                    status = await self._relay.pairing_status()
                except Exception as status_exc:
                    LOG.debug(
                        "browser_extension_pairing_status_failed",
                        error_type=type(status_exc).__name__,
                    )
                    return False
                if status.get("owned") is True:
                    return True
                if status.get("active") is True:
                    await asyncio.sleep(min(0.1, remaining))
                continue
            except Exception as exc:
                LOG.debug("browser_extension_pairing_begin_failed", error_type=type(exc).__name__)
                return False
            if result.get("opened") is True:
                return True
            pairing_url = result.get("pairingUrl")
            return isinstance(pairing_url, str) and self.open_extension_url(pairing_url)

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


def _create_adapter(registry: VirtualTargetRegistry, relay: _Relay) -> _Adapter:
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


async def _cleanup_failed_start(relay: _Relay, adapter: _Adapter) -> None:
    try:
        await relay.stop()
    except Exception:
        LOG.exception("failed to stop browser extension relay after startup error")
    try:
        await adapter.stop()
    except Exception:
        LOG.exception("failed to stop browser extension adapter after startup error")


def broker_mode_enabled() -> bool:
    return _broker_requested() and _broker_platform_supported()


def _broker_requested() -> bool:
    return os.environ.get(_BROKER_ENV) != "0"


def _broker_platform_supported() -> bool:
    return os.name == "posix" and sys.platform != "win32"
