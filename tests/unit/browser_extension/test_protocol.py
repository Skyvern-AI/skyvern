from __future__ import annotations

import json

import pytest

from skyvern.browser_extension.errors import BrowserExtensionError
from skyvern.browser_extension.protocol import (
    ALLOWED_CDP_METHOD_PREFIXES,
    ALLOWED_EVENTS,
    ALLOWED_OPS,
    DENIED_CDP_METHODS,
    ERROR_CODES,
    EXTENSION_ID,
    PROTOCOL_VERSION,
    build_request,
    is_cdp_method_allowed,
    is_restricted_url,
    parse_extension_message,
)


def test_parse_valid_response() -> None:
    parsed = parse_extension_message(
        json.dumps({"v": 1, "type": "response", "id": "r-1", "ok": True, "result": {"tabId": 12}})
    )

    assert parsed.kind == "response"
    assert parsed.request_id == "r-1"
    assert parsed.ok is True
    assert parsed.result == {"tabId": 12}
    assert parsed.error_code is None


def test_parse_valid_event() -> None:
    parsed = parse_extension_message(
        json.dumps(
            {
                "v": 1,
                "type": "event",
                "event": "scope.tabAdded",
                "params": {"tabId": 12, "url": "https://example.com", "title": "Example"},
            }
        )
    )

    assert parsed.kind == "event"
    assert parsed.event == "scope.tabAdded"
    assert parsed.params == {"tabId": 12, "url": "https://example.com", "title": "Example"}


def test_parse_valid_ping() -> None:
    parsed = parse_extension_message('{"v":1,"type":"ping"}')

    assert parsed.kind == "ping"


def test_unknown_message_type_raises() -> None:
    with pytest.raises(BrowserExtensionError):
        parse_extension_message('{"v":1,"type":"auth.ok"}')


def test_protocol_allowlists_match_contract() -> None:
    assert PROTOCOL_VERSION == 1
    assert EXTENSION_ID == "fmamdhmfeihjjaiheideemihnbpnokin"
    assert ALLOWED_OPS == frozenset(
        {
            "debugger.attach",
            "debugger.detach",
            "debugger.send",
            "tabs.create",
            "tabs.remove",
            "tabs.activate",
            "tabs.list",
        }
    )
    assert ALLOWED_EVENTS == frozenset(
        {
            "extension.hello",
            "debugger.event",
            "debugger.detached",
            "scope.tabAdded",
            "scope.tabRemoved",
            "tabs.created",
        }
    )
    assert ERROR_CODES == frozenset(
        {
            "AUTH_FAILED",
            "OP_NOT_ALLOWED",
            "TAB_NOT_FOUND",
            "TAB_NOT_SCOPED",
            "RESTRICTED_URL",
            "DEBUGGER_DETACHED",
            "CDP_METHOD_NOT_ALLOWED",
            "CDP_ERROR",
            "INTERNAL",
        }
    )


def test_build_request_checks_operation_allowlist() -> None:
    assert build_request("r-3", "tabs.list", {}) == {
        "v": 1,
        "type": "request",
        "id": "r-3",
        "op": "tabs.list",
        "args": {},
    }

    with pytest.raises(BrowserExtensionError):
        build_request("r-4", "cookies.read", {})
    with pytest.raises(BrowserExtensionError):
        build_request(4, "tabs.list", {})  # type: ignore[arg-type]


def test_cdp_method_allowlist() -> None:
    assert all(prefix.endswith(".") for prefix in ALLOWED_CDP_METHOD_PREFIXES)
    assert is_cdp_method_allowed("Network.enable")
    assert is_cdp_method_allowed("Storage.getUsageAndQuota")
    assert is_cdp_method_allowed("Network.getCookies")
    assert not is_cdp_method_allowed("Network.getCookies", {"urls": []})
    assert not is_cdp_method_allowed("Browser.close")
    assert not is_cdp_method_allowed("SystemInfo.getInfo")
    assert not is_cdp_method_allowed("PageX.navigate")


@pytest.mark.parametrize(
    "method",
    [
        "Network.getAllCookies",
        "Network.clearBrowserCookies",
        "Network.clearBrowserCache",
        "Storage.getCookies",
        "Storage.setCookies",
        "Storage.clearCookies",
    ],
)
def test_cdp_method_denylist(method: str) -> None:
    assert method in DENIED_CDP_METHODS
    assert not is_cdp_method_allowed(method)


@pytest.mark.parametrize(
    ("url", "restricted"),
    [
        ("chrome://settings", True),
        ("about:blank", False),
        ("about:config", True),
        ("https://chromewebstore.google.com/detail/example", True),
        ("https://chromewebstore.google.com./detail/x", True),
        ("https://example.com", False),
        ("file:///tmp/example.html", True),
    ],
)
def test_restricted_url_matrix(url: str, restricted: bool) -> None:
    assert is_restricted_url(url) is restricted
