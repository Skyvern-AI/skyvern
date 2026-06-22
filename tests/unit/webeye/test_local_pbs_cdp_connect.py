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
        cdp_connect_headers={"x-api-key": "test-key"},
    )
    assert connect_url == "ws://127.0.0.1:9224/devtools/browser/abc"
    assert headers == {"x-api-key": "test-key", "X-Session-Id": "pbs_123"}


@patch("skyvern.webeye.cdp_connection.settings.ENV", "prod")
def test_resolve_local_pbs_cdp_url_noop_outside_local() -> None:
    url = "wss://sessions.example.com/pbs_1/token/devtools/browser/abc"
    assert resolve_local_pbs_cdp_url(url) == url
