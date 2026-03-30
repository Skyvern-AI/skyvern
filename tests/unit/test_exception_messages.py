from skyvern.exceptions import (
    SkyvernException,
    UnknownErrorWhileCreatingBrowserContext,
    get_user_facing_exception_message,
)


class FakePatchrightTimeoutError(Exception):
    pass


def test_unknown_error_while_creating_browser_context_strips_call_log() -> None:
    inner_exception = FakePatchrightTimeoutError(
        "BrowserType.launch_persistent_context: Timeout 180000ms exceeded. "
        "Call log:\n- <launching> /opt/microsoft/msedge/msedge --proxy-server=http://network.joinmassive.com:65534"
    )

    error = UnknownErrorWhileCreatingBrowserContext("dynamic-browser", inner_exception)
    message = str(error)

    assert "Call log:" not in message
    assert "--proxy-server=" not in message
    assert "timed out after 180 seconds" in message
    assert "Please try re-running." in message
    assert "support@skyvern.com" in message


def test_get_user_facing_exception_message_for_skyvern_exception() -> None:
    message = get_user_facing_exception_message(SkyvernException("Human-friendly message"))
    assert message == "Human-friendly message"


def test_get_user_facing_exception_message_for_generic_exception() -> None:
    message = get_user_facing_exception_message(ValueError("raw error"))
    assert message == "Unexpected error: raw error"


def test_browser_connection_error_connect_over_cdp_websocket() -> None:
    """The exact error from SKY-8578: connect_over_cdp fails with 502 Bad Gateway."""
    raw_error = (
        "BrowserType.connect_over_cdp: WebSocket error: "
        "wss://sessions.skyvern.com/pbs_510103089551940236/"
        "1c41-4113-9f69-44ed13f3cc40 502 Bad Gateway "
        "<html><head><title>502 Bad Gateway</title></head></html> "
        "Call log: - <ws connecting> wss://sessions.skyvern.com/pbs_510103089551940236 "
        "- <ws unexpected response> 502 Bad Gateway "
        "- <ws error> error WebSocket was closed before the connection code=1006 reason="
    )
    message = get_user_facing_exception_message(Exception(raw_error))
    assert "sessions.skyvern.com" not in message
    assert "502 Bad Gateway" not in message
    assert "Call log" not in message
    assert "WebSocket" not in message
    assert "Failed to connect to the browser session" in message
    assert "try re-running" in message


def test_browser_connection_error_websocket_closed() -> None:
    """WebSocket closed before connection is established."""
    raw_error = "WebSocket was closed before the connection was established"
    message = get_user_facing_exception_message(Exception(raw_error))
    assert "Failed to connect to the browser session" in message
    assert "try re-running" in message


def test_non_browser_error_not_intercepted() -> None:
    """Regular errors should still pass through as-is."""
    message = get_user_facing_exception_message(ValueError("some other error"))
    assert message == "Unexpected error: some other error"
