from __future__ import annotations

import asyncio
import errno
import subprocess
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

import skyvern.browser_extension.runtime as runtime_module
from skyvern.browser_extension.broker.client import LegacyBridgeOwnerError
from skyvern.browser_extension.errors import BrowserExtensionError
from skyvern.browser_extension.runtime import BrowserExtensionRuntime


class StubRelay:
    def __init__(
        self,
        token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
        *,
        calls: list[str],
        start_error: BaseException | None = None,
    ) -> None:
        self.token = token
        self.port = port
        self.bound_port = port
        self.on_event = on_event
        self.on_disconnect = on_disconnect
        self.calls = calls
        self.start_error = start_error
        self.connected = True
        self.stop_count = 0

    async def acquire_pairing_nonce(self) -> str:
        return "runtime-pairing-nonce"

    async def start(self) -> None:
        self.calls.append("relay.start")
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.calls.append("relay.stop")
        self.stop_count += 1

    async def wait_connected(self, timeout: float) -> bool:
        return self.connected


class StubAdapter:
    def __init__(self, registry, relay: StubRelay, *, calls: list[str]) -> None:
        self.registry = registry
        self.relay = relay
        self.calls = calls
        self.events: list[tuple[str, dict]] = []
        self.disconnect_count = 0
        self.stop_count = 0
        self.cdp_ws_url = "ws://127.0.0.1:23456/cdp/test-capability"

    async def start(self) -> None:
        self.calls.append("adapter.start")

    async def stop(self) -> None:
        self.calls.append("adapter.stop")
        self.stop_count += 1

    async def handle_extension_event(self, event: str, params: dict) -> None:
        self.events.append((event, params))

    async def on_extension_disconnect(self) -> None:
        self.disconnect_count += 1


@pytest_asyncio.fixture(autouse=True)
async def reset_runtime() -> AsyncGenerator[None]:
    BrowserExtensionRuntime._instance = None
    BrowserExtensionRuntime._lock = asyncio.Lock()
    yield
    instance = BrowserExtensionRuntime.instance()
    if instance is not None:
        await instance.shutdown()


def install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    relay_start_error: BaseException | None = None,
) -> tuple[list[StubRelay], list[StubAdapter], list[str]]:
    relays: list[StubRelay] = []
    adapters: list[StubAdapter] = []
    calls: list[str] = []

    def relay_factory(token, port, on_event, on_disconnect) -> StubRelay:
        relay = StubRelay(
            token,
            port,
            on_event,
            on_disconnect,
            calls=calls,
            start_error=relay_start_error,
        )
        relays.append(relay)
        return relay

    def adapter_factory(registry, relay) -> StubAdapter:
        adapter = StubAdapter(registry, relay, calls=calls)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(runtime_module, "_relay_factory", relay_factory)
    monkeypatch.setattr(runtime_module, "_broker_factory", relay_factory)
    monkeypatch.setattr(runtime_module, "_adapter_factory", adapter_factory)
    monkeypatch.setattr(runtime_module, "load_or_create_pairing_token", lambda: "runtime-test-token")
    return relays, adapters, calls


@pytest.mark.asyncio
async def test_singleton_is_idempotent_and_late_binds_adapter_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    relays, adapters, calls = install_stubs(monkeypatch)

    first = await BrowserExtensionRuntime.get_or_start(21001)
    second = await BrowserExtensionRuntime.get_or_start(21002)

    assert first is second
    assert BrowserExtensionRuntime.instance() is first
    assert len(relays) == len(adapters) == 1
    assert relays[0].port == 21001
    assert calls == ["adapter.start", "relay.start"]
    assert first.cdp_ws_url == adapters[0].cdp_ws_url
    assert first.extension_connected
    assert await first.wait_for_extension(0.01)

    event_params = {"tabId": 17}
    await relays[0].on_event("scope.tabAdded", event_params)
    assert adapters[0].events == [("scope.tabAdded", event_params)]
    assert relays[0].on_disconnect is not None
    await relays[0].on_disconnect()
    assert adapters[0].disconnect_count == 1


@pytest.mark.asyncio
async def test_open_pairing_page_uses_relay_nonce_without_exposing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stubs(monkeypatch)
    opener = MagicMock(return_value=True)
    monkeypatch.setattr(BrowserExtensionRuntime, "open_extension_url", staticmethod(opener))
    runtime = await BrowserExtensionRuntime.get_or_start(21003)

    assert await runtime.open_pairing_page()
    assert await runtime.open_pairing_page()
    opener.assert_called_with("http://127.0.0.1:21003/pair#runtime-pairing-nonce")
    assert opener.call_count == 2
    assert "runtime-test-token" not in opener.call_args.args[0]


@pytest.mark.asyncio
async def test_broker_is_the_default_transport_and_the_env_flag_opts_back_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen: list[str] = []

    def make_factory(name: str):
        def factory(token, port, on_event, on_disconnect) -> StubRelay:
            chosen.append(name)
            return StubRelay(token, port, on_event, on_disconnect, calls=[])

        return factory

    monkeypatch.setattr(runtime_module, "_relay_factory", make_factory("relay"))
    monkeypatch.setattr(runtime_module, "_broker_factory", make_factory("broker"))
    monkeypatch.setattr(
        runtime_module, "_adapter_factory", lambda registry, relay: StubAdapter(registry, relay, calls=[])
    )
    monkeypatch.setattr(runtime_module, "load_or_create_pairing_token", lambda: "runtime-test-token")

    brokered = await BrowserExtensionRuntime.get_or_start(21010)
    assert brokered.brokered
    await brokered.shutdown()

    monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_BROKER", "0")
    legacy = await BrowserExtensionRuntime.get_or_start(21011)
    assert not legacy.brokered
    await legacy.shutdown()

    assert chosen == ["broker", "relay"]


@pytest.mark.asyncio
async def test_legacy_bridge_owner_explains_that_the_other_session_cannot_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, adapters, _ = install_stubs(monkeypatch, relay_start_error=LegacyBridgeOwnerError("held"))

    with pytest.raises(BrowserExtensionError) as error_info:
        await BrowserExtensionRuntime.get_or_start(23002)

    message = str(error_info.value)
    assert "23002" in message
    assert "older Skyvern MCP session" in message
    assert "restart that session" in message
    assert adapters[0].stop_count == 1
    assert BrowserExtensionRuntime.instance() is None


def test_open_extension_url_targets_google_chrome_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    run = MagicMock()
    monkeypatch.setattr(runtime_module.sys, "platform", "darwin")
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/open" if name == "open" else None)
    monkeypatch.setattr(runtime_module.subprocess, "run", run)

    assert BrowserExtensionRuntime.open_extension_url("http://127.0.0.1:19777/pair#nonce")
    run.assert_called_once_with(
        ["/usr/bin/open", "-a", "Google Chrome", "http://127.0.0.1:19777/pair#nonce"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_open_extension_url_launches_direct_browser_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
) -> None:
    popen = MagicMock()
    monkeypatch.setattr(runtime_module.sys, "platform", platform)
    monkeypatch.setattr(runtime_module.subprocess, "Popen", popen)
    if platform == "linux":
        executable = Path("/usr/bin/chromium")
        monkeypatch.setattr(
            runtime_module.shutil, "which", lambda name: str(executable) if name == "chromium" else None
        )
        platform_options = {"start_new_session": True}
    else:
        executable = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.delenv("PROGRAMFILES", raising=False)
        monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
        platform_options = {"creationflags": 0x00000208}

    assert BrowserExtensionRuntime.open_extension_url("http://127.0.0.1:19777/pair#nonce")
    popen.assert_called_once_with(
        [str(executable), "http://127.0.0.1:19777/pair#nonce"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **platform_options,
    )


@pytest.mark.asyncio
async def test_port_resolution_prefers_explicit_then_environment_then_default(monkeypatch: pytest.MonkeyPatch) -> None:
    relays, _, _ = install_stubs(monkeypatch)
    monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_PORT", "22001")

    environment_runtime = await BrowserExtensionRuntime.get_or_start()
    assert relays[-1].port == 22001
    await environment_runtime.shutdown()

    explicit_runtime = await BrowserExtensionRuntime.get_or_start(22002)
    assert relays[-1].port == 22002
    await explicit_runtime.shutdown()

    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_PORT")
    default_runtime = await BrowserExtensionRuntime.get_or_start()
    assert relays[-1].port == 19777
    await default_runtime.shutdown()


@pytest.mark.asyncio
async def test_port_in_use_has_actionable_browser_extension_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _, adapters, _ = install_stubs(monkeypatch, relay_start_error=OSError(errno.EADDRINUSE, "address in use"))

    with pytest.raises(BrowserExtensionError) as error_info:
        await BrowserExtensionRuntime.get_or_start(23001)

    message = str(error_info.value)
    assert "23001" in message
    assert "SKYVERN_BROWSER_EXTENSION_PORT" in message
    assert "extension popup" in message
    assert adapters[0].stop_count == 1
    assert BrowserExtensionRuntime.instance() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("port_value", [0, "0"])
async def test_zero_port_is_rejected(monkeypatch: pytest.MonkeyPatch, port_value: int | str) -> None:
    install_stubs(monkeypatch)
    if isinstance(port_value, str):
        monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_PORT", port_value)
        port = None
    else:
        port = port_value

    with pytest.raises(BrowserExtensionError, match="between 1 and 65535"):
        await BrowserExtensionRuntime.get_or_start(port)


@pytest.mark.asyncio
async def test_shutdown_stops_relay_before_adapter_is_idempotent_and_resets_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relays, adapters, calls = install_stubs(monkeypatch)
    runtime = await BrowserExtensionRuntime.get_or_start(24001)

    await runtime.shutdown()
    await runtime.shutdown()

    assert calls == ["adapter.start", "relay.start", "relay.stop", "adapter.stop"]
    assert relays[0].stop_count == adapters[0].stop_count == 1
    assert BrowserExtensionRuntime.instance() is None

    restarted = await BrowserExtensionRuntime.get_or_start(24002)
    assert restarted is not runtime
    assert relays[-1].port == 24002


def test_extension_dir_points_to_packaged_manifest() -> None:
    directory = BrowserExtensionRuntime.extension_dir()
    expected_directory = Path(runtime_module.__file__).resolve().parent / "extension"

    assert directory == expected_directory
    if not (directory / "manifest.json").exists():
        pytest.skip("extension manifest is owned by another build stream")
    assert directory.is_dir()
