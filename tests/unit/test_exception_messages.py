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
