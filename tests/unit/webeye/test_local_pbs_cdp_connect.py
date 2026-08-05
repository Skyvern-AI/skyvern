import os
from unittest.mock import patch

from skyvern.webeye.cdp_connection import (
    prepare_persistent_browser_cdp_connect,
    resolve_local_pbs_cdp_url,
    strip_browser_address_discriminator,
)


def test_strip_browser_address_discriminator() -> None:
    url = "ws://127.0.0.1:9224/devtools/browser/abc#pbs_pbs_123"
    assert strip_browser_address_discriminator(url) == "ws://127.0.0.1:9224/devtools/browser/abc"


@patch.dict(os.environ, {"LOCAL_CDP_HOST_PORT": "9224"}, clear=False)
@patch("skyvern.webeye.cdp_connection.settings.ENV", "local")
def test_prepare_local_pbs_cdp_connect_rewrites_port_and_adds_session_header() -> None:
    browser_address = "ws://127.0.0.1:9222/devtools/browser/abc#pbs_pbs_123"
    connect_url, headers = prepare_persistent_browser_cdp_connect(
        browser_address,
        browser_session_id="pbs_123",
        x_api_key="managed-key",
        cdp_connect_headers={"X-Provider-Auth": "provider-value"},
    )
    assert connect_url == "ws://127.0.0.1:9224/devtools/browser/abc"
    assert headers == {
        "X-Provider-Auth": "provider-value",
        "x-api-key": "managed-key",
        "X-Session-Id": "pbs_123",
    }


@patch("skyvern.webeye.cdp_connection.settings.ENV", "prod")
def test_prepare_resolved_runner_proxy_adds_managed_headers() -> None:
    browser_address = "wss://proxy.example.test/pbs_123"
    connect_url, headers = prepare_persistent_browser_cdp_connect(
        browser_address,
        browser_session_id="pbs_123",
        x_api_key="managed-key",
        cdp_connect_headers={"X-Provider-Auth": "provider-value"},
        is_resolved_runner_cdp_proxy=True,
    )

    assert connect_url == browser_address
    assert headers == {
        "X-Provider-Auth": "provider-value",
        "x-api-key": "managed-key",
        "X-Session-Id": "pbs_123",
    }


@patch("skyvern.webeye.cdp_connection.settings.ENV", "prod")
def test_prepare_managed_session_router_adds_managed_headers() -> None:
    browser_address = "wss://session-router.example.test/pbs_123/routing-token/devtools/browser/browser-id"
    connect_url, headers = prepare_persistent_browser_cdp_connect(
        browser_address,
        browser_session_id="pbs_123",
        x_api_key="managed-key",
        is_managed_session_router=True,
    )

    assert connect_url == browser_address
    assert headers == {
        "x-api-key": "managed-key",
        "X-Session-Id": "pbs_123",
    }


@patch("skyvern.webeye.cdp_connection.settings.ENV", "prod")
def test_prepare_session_router_lookalike_does_not_add_managed_headers() -> None:
    browser_address = "wss://remote.example.test/pbs_123/routing-token/devtools/browser/browser-id"
    connect_url, headers = prepare_persistent_browser_cdp_connect(
        browser_address,
        browser_session_id="pbs_123",
        x_api_key="managed-key",
    )

    assert connect_url == browser_address
    assert headers is None


@patch("skyvern.webeye.cdp_connection.settings.ENV", "prod")
def test_prepare_remote_cdp_connect_does_not_add_managed_headers() -> None:
    browser_address = "wss://browser.example.test/devtools/browser/id"
    connect_url, headers = prepare_persistent_browser_cdp_connect(
        browser_address,
        browser_session_id="pbs_123",
        x_api_key="managed-key",
        cdp_connect_headers={"X-Provider-Auth": "provider-value"},
    )

    assert connect_url == browser_address
    assert headers == {"X-Provider-Auth": "provider-value"}


@patch("skyvern.webeye.cdp_connection.settings.ENV", "prod")
def test_resolve_local_pbs_cdp_url_noop_outside_local() -> None:
    url = "wss://sessions.example.com/pbs_1/token/devtools/browser/abc"
    assert resolve_local_pbs_cdp_url(url) == url
