from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.routes.streaming.channels import vnc as vnc_module


class ConnectedForTest(Exception):
    """Stop the stream immediately after capturing the upstream connection."""


class CapturingConnection:
    def __init__(self, capture: dict[str, Any], url: str, additional_headers: dict[str, str]) -> None:
        self.capture = capture
        self.capture["url"] = url
        self.capture["additional_headers"] = additional_headers

    async def __aenter__(self) -> None:
        raise ConnectedForTest

    async def __aexit__(self, *args: object) -> None:
        return None


async def _capture_upstream_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    browser_address: str,
    ip_address: str | None = None,
    persisted_vnc_port: int | None = None,
    global_vnc_port: int = 6080,
    x_api_key: str = "secret",
) -> dict[str, Any]:
    capture: dict[str, Any] = {}

    def connect(url: str, *, additional_headers: dict[str, str]) -> CapturingConnection:
        return CapturingConnection(capture, url, additional_headers)

    monkeypatch.setattr(vnc_module.websockets, "connect", connect)
    channel = SimpleNamespace(
        browser_session=SimpleNamespace(
            browser_address=browser_address,
            ip_address=ip_address,
            vnc_port=persisted_vnc_port,
        ),
        class_name="VncChannel",
        identity={"browser_session_id": "pbs_test", "organization_id": "org_test"},
        vnc_port=global_vnc_port,
        x_api_key=x_api_key,
    )

    with pytest.raises(ConnectedForTest):
        await vnc_module.loop_stream_vnc(channel)

    return capture


@pytest.mark.asyncio
async def test_v2_routed_address_wins_over_persisted_local_port(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = await _capture_upstream_connection(
        monkeypatch,
        browser_address="wss://browser.example/pbs_remote/token/devtools/browser/browser-id",
        persisted_vnc_port=6087,
    )

    assert capture == {
        "url": "wss://browser.example/vnc/pbs_remote/token",
        "additional_headers": {"x-api-key": "secret"},
    }


@pytest.mark.asyncio
async def test_v1_ip_wins_over_persisted_local_port_and_uses_global_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = await _capture_upstream_connection(
        monkeypatch,
        browser_address="",
        ip_address="10.0.0.4:9222",
        persisted_vnc_port=6087,
    )

    assert capture == {"url": "ws://10.0.0.4:6080", "additional_headers": {}}


@pytest.mark.asyncio
async def test_addressless_local_session_uses_persisted_vnc_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vnc_module.settings, "BROWSER_STREAMING_MODE", "vnc")
    capture = await _capture_upstream_connection(
        monkeypatch,
        browser_address="",
        persisted_vnc_port=6087,
    )

    assert capture == {"url": "ws://127.0.0.1:6087", "additional_headers": {}}


@pytest.mark.asyncio
async def test_addressless_cdp_session_does_not_use_persisted_vnc_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vnc_module.settings, "BROWSER_STREAMING_MODE", "cdp")
    capture = await _capture_upstream_connection(
        monkeypatch,
        browser_address="",
        persisted_vnc_port=6087,
    )

    assert capture == {"url": "ws://None:6080", "additional_headers": {}}


@pytest.mark.asyncio
async def test_ordinary_browser_address_keeps_hostname_and_global_port(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = await _capture_upstream_connection(
        monkeypatch,
        browser_address="ws://browser.internal:9222/devtools/browser/browser-id",
    )

    assert capture == {"url": "ws://browser.internal:6080", "additional_headers": {}}
