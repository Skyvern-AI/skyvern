from http import HTTPStatus

import pytest

from skyvern.exceptions import (
    CdpConnectionConfigurationError,
    SkyvernException,
    SkyvernExtraNotInstalled,
    SkyvernHTTPException,
    UnknownErrorWhileCreatingBrowserContext,
    get_user_facing_exception_message,
    raise_server_extra_required,
    require_server_extra_modules,
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


def test_unknown_error_preserves_cdp_configuration_guidance() -> None:
    inner_exception = CdpConnectionConfigurationError(
        "Skyvern reached the configured CDP address, but /json/version returned HTTP 404. "
        "Start Chrome with --remote-debugging-port=9222."
    )

    error = UnknownErrorWhileCreatingBrowserContext("cdp-connect", inner_exception)
    message = str(error)

    assert "CdpConnectionConfigurationError" in message
    assert "/json/version returned HTTP 404" in message
    assert "--remote-debugging-port=9222" in message


def test_get_user_facing_exception_message_for_skyvern_exception() -> None:
    message = get_user_facing_exception_message(SkyvernException("Human-friendly message"))
    assert message == "Human-friendly message"


def test_get_user_facing_exception_message_for_generic_exception() -> None:
    message = get_user_facing_exception_message(ValueError("raw error"))
    assert message == "Unexpected error: raw error"


def test_skyvern_http_exception_normalizes_status_code_to_plain_int() -> None:
    error = SkyvernHTTPException("bad request", status_code=HTTPStatus.BAD_REQUEST)

    assert error.status_code == 400
    assert type(error.status_code) is int


def test_raise_server_extra_required_translates_when_server_extra_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.exceptions.find_spec",
        lambda module_name: None,
    )
    missing = ModuleNotFoundError("No module named 'starlette_context'", name="starlette_context")

    with pytest.raises(SkyvernExtraNotInstalled, match=r"pip install skyvern\[server\]"):
        raise_server_extra_required("skyvern.library.skyvern_browser", missing)


def test_raise_server_extra_required_translates_missing_server_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.exceptions.find_spec",
        lambda module_name: None if module_name == "playwright" else object(),
    )
    missing = ModuleNotFoundError("No module named 'playwright'", name="playwright")

    with pytest.raises(SkyvernExtraNotInstalled, match=r"pip install skyvern\[server\]"):
        raise_server_extra_required("skyvern.library.skyvern_browser", missing)


def test_raise_server_extra_required_preserves_installed_marker_submodule_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.exceptions.find_spec", lambda module_name: object())
    missing = ModuleNotFoundError("No module named 'playwright._impl._broken'", name="playwright._impl._broken")

    with pytest.raises(ModuleNotFoundError) as exc_info:
        raise_server_extra_required("skyvern.library.skyvern_browser", missing)

    assert exc_info.value is missing


def test_raise_server_extra_required_preserves_unknown_missing_dependency_when_server_extra_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skyvern.exceptions.find_spec",
        lambda module_name: None if module_name == "playwright" else object(),
    )
    missing = ModuleNotFoundError("No module named 'bogus_internal_dep'", name="bogus_internal_dep")

    with pytest.raises(ModuleNotFoundError) as exc_info:
        raise_server_extra_required("skyvern.services.script_service", missing)

    assert exc_info.value is missing


def test_raise_server_extra_required_preserves_missing_dependency_when_server_markers_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.exceptions.find_spec", lambda module_name: object())
    missing = ModuleNotFoundError("No module named 'bogus_internal_dep'", name="bogus_internal_dep")

    with pytest.raises(ModuleNotFoundError) as exc_info:
        raise_server_extra_required("skyvern.services.script_service", missing)

    assert exc_info.value is missing


def test_raise_server_extra_required_preserves_internal_skyvern_import_failure_when_server_extra_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skyvern.exceptions.find_spec",
        lambda module_name: None if module_name == "playwright" else object(),
    )
    missing = ModuleNotFoundError("No module named 'skyvern.typo'", name="skyvern.typo")

    with pytest.raises(ModuleNotFoundError) as exc_info:
        raise_server_extra_required("skyvern.services.script_service", missing)

    assert exc_info.value is missing


def test_require_server_extra_modules_requires_server_sentinels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.exceptions.find_spec",
        lambda module_name: None if module_name == "sqlalchemy" else object(),
    )

    with pytest.raises(SkyvernExtraNotInstalled, match=r"pip install skyvern\[server\]"):
        require_server_extra_modules("skyvern.library.skyvern_browser_page")


def test_require_server_extra_modules_catches_partial_server_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.exceptions.find_spec",
        lambda module_name: None if module_name == "jinja2" else object(),
    )

    with pytest.raises(SkyvernExtraNotInstalled, match=r"pip install skyvern\[server\]"):
        require_server_extra_modules("skyvern.library.skyvern_browser_page")


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


def test_unknown_error_display_server_missing_xserver() -> None:
    inner_exception = Exception(
        "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n\n"
        "Browser logs:\n"
        "Looks like you launched a headed browser without having a XServer running.\n"
        "[err] Missing X server or $DISPLAY\n"
        "[err] ui/aura/env.cc: The platform failed to initialize. Exiting."
    )
    error = UnknownErrorWhileCreatingBrowserContext("dynamic-browser", inner_exception)
    message = str(error)
    assert "browser display/graphics stack" in message
    assert "browser-environment issue" in message
    assert "support@skyvern.com" in message


def test_unknown_error_display_server_platform_failed() -> None:
    inner_exception = Exception("[err] ui/aura/env.cc: The platform failed to initialize. Exiting.")
    error = UnknownErrorWhileCreatingBrowserContext("dynamic-browser", inner_exception)
    message = str(error)
    assert "browser display/graphics stack" in message


def test_unknown_error_display_server_egl_failure() -> None:
    inner_exception = Exception(
        "[err] [297028:297028:0407/015340.854525:ERROR:ui/gl/gl_surface_egl.cc:1013] "
        "No suitable EGL configs found for initialization.\n"
        "[err] [297028:297028:0407/015340.854713:ERROR:gpu/ipc/service/gpu_init.cc:118] "
        "CollectGraphicsInfo failed."
    )
    error = UnknownErrorWhileCreatingBrowserContext("dynamic-browser", inner_exception)
    message = str(error)
    assert "browser display/graphics stack" in message
    assert "browser profile problem" in message


def test_unknown_error_display_server_no_display() -> None:
    inner_exception = Exception("No display environment variable set")
    error = UnknownErrorWhileCreatingBrowserContext("dynamic-browser", inner_exception)
    message = str(error)
    assert "browser display/graphics stack" in message


def test_unknown_error_strips_browser_logs_with_internal_path() -> None:
    """SKY-8931: Browser logs section exposes internal browser binary path."""
    inner_exception = Exception(
        "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n\n"
        "Browser logs:\n"
        "<launching> /opt/internal-browser/chromium/chrome "
        "--disable-field-trial-config --disable-background-networking"
    )
    error = UnknownErrorWhileCreatingBrowserContext("dynamic-browser", inner_exception)
    message = str(error)
    assert "/opt/internal-browser" not in message
    assert "Browser logs:" not in message
    assert "--disable-field-trial-config" not in message
    # SKY-9319: TargetClosedError-style failures now return a friendly retry message
    # instead of the raw Playwright string.
    assert "The browser closed unexpectedly during launch" in message
    assert "support@skyvern.com" in message


def test_unknown_error_timeout_with_browser_logs_still_formats_structured() -> None:
    """Timeout + Browser logs: the structured 'timed out after N seconds' path must win."""
    inner_exception = Exception(
        "BrowserType.launch_persistent_context: Timeout 180000ms exceeded.\n\n"
        "Browser logs:\n"
        "<launching> /opt/internal-browser/chromium/chrome --disable-field-trial-config"
    )
    error = UnknownErrorWhileCreatingBrowserContext("dynamic-browser", inner_exception)
    message = str(error)
    assert "timed out after 180 seconds" in message
    assert "/opt/internal-browser" not in message
    assert "Browser logs:" not in message
