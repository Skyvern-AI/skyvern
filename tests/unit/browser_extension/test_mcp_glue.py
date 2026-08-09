from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call

import pytest
import typer
from typer.testing import CliRunner

from skyvern.browser_extension.errors import BrowserExtensionError
from skyvern.browser_extension.runtime import BrowserExtensionRuntime
from skyvern.cli import run_commands
from skyvern.cli.commands import browser as browser_commands
from skyvern.cli.commands.browser import browser_app
from skyvern.cli.core import session_manager
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import session as mcp_session

_PAIRING_OPENED_GUIDANCE = (
    "Skyvern browser extension is not connected. A secure pairing tab was opened. Approve the connection, approve "
    "pairing in the Skyvern Agent confirmation tab, and retry."
)
_PAIRING_FALLBACK_GUIDANCE = (
    "Skyvern browser extension is not connected and the pairing tab could not be opened automatically. Run "
    "`skyvern browser extension-pair`, approve the connection, approve pairing in the Skyvern Agent confirmation "
    "tab, and retry."
)


@pytest.fixture(autouse=True)
def _use_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_manager, "_stateless_http_mode", False)


@pytest.mark.parametrize("pairing_opened", [True, False])
def test_extension_not_connected_guidance_omits_manual_token_flow(pairing_opened: bool) -> None:
    guidance = mcp_session._extension_not_connected_guidance(pairing_opened=pairing_opened)

    assert "extension-token" not in guidance
    assert "paste the token" not in guidance


@pytest.mark.parametrize(
    ("browser_type", "expected"),
    [
        (None, False),
        ("", False),
        ("cdp-connect", False),
        ("extension-connect", True),
        ("Extension-Connect", False),
    ],
)
def test_should_default_to_extension_env_matrix(
    monkeypatch: pytest.MonkeyPatch,
    browser_type: str | None,
    expected: bool,
) -> None:
    if browser_type is None:
        monkeypatch.setenv("BROWSER_TYPE", "leak-guard-sentinel")
        monkeypatch.delenv("BROWSER_TYPE", raising=False)
    else:
        monkeypatch.setenv("BROWSER_TYPE", browser_type)

    assert mcp_session._should_default_to_extension() is expected


@pytest.mark.asyncio
async def test_explicit_local_session_does_not_start_extension_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = MagicMock(app_url=None)
    do_session_create = AsyncMock(return_value=(browser, SimpleNamespace(local=True, headless=False)))
    get_or_start = AsyncMock()
    monkeypatch.setenv("BROWSER_TYPE", "extension-connect")
    monkeypatch.setattr(mcp_session, "get_skyvern", MagicMock())
    monkeypatch.setattr(mcp_session, "do_session_create", do_session_create)
    monkeypatch.setattr(mcp_session, "set_current_session", MagicMock())
    monkeypatch.setattr(mcp_session.BrowserExtensionRuntime, "get_or_start", get_or_start)

    result = await mcp_session.skyvern_browser_session_create(local=True)

    assert result["ok"] is True
    assert result["data"] == {"local": True, "headless": False}
    get_or_start.assert_not_awaited()
    do_session_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_extension_session_rejects_stateless_http_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    get_or_start = AsyncMock()
    monkeypatch.setenv("BROWSER_TYPE", "extension-connect")
    monkeypatch.setattr(mcp_session.BrowserExtensionRuntime, "get_or_start", get_or_start)
    session_manager.set_stateless_http_mode(True)
    try:
        result = await mcp_session.skyvern_browser_session_create()
    finally:
        session_manager.set_stateless_http_mode(False)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_session.ErrorCode.SDK_ERROR
    assert result["error"]["message"] == (
        "The Skyvern browser extension requires the MCP server to run on the stdio transport. "
        "Restart with: skyvern mcp --browser-extension"
    )
    get_or_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_create_extension_takes_precedence_over_cdp(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = SimpleNamespace(wait_for_extension=AsyncMock(return_value=True))
    get_or_start = AsyncMock(return_value=runtime)
    resolve_browser = AsyncMock(return_value=(MagicMock(), BrowserContext(mode="extension")))
    monkeypatch.setattr(mcp_session, "_should_default_to_extension", lambda: True)
    monkeypatch.setattr(mcp_session, "_should_default_to_cdp", lambda: (True, "ws://cdp.example.test"))
    monkeypatch.setattr(mcp_session.BrowserExtensionRuntime, "get_or_start", get_or_start)
    monkeypatch.setattr(mcp_session, "resolve_browser", resolve_browser)

    result = await mcp_session.skyvern_browser_session_create()

    assert result["ok"] is True
    assert result["data"]["browser"] == "extension"
    assert result["data"]["session"] == "implicit"
    get_or_start.assert_awaited_once_with()
    runtime.wait_for_extension.assert_awaited_once_with(10.0)
    resolve_browser.assert_awaited_once_with(extension_runtime=runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pairing_opened", "expected_guidance"),
    [(True, _PAIRING_OPENED_GUIDANCE), (False, _PAIRING_FALLBACK_GUIDANCE)],
)
async def test_session_create_extension_not_connected_returns_pinned_guidance(
    monkeypatch: pytest.MonkeyPatch,
    pairing_opened: bool,
    expected_guidance: str,
) -> None:
    runtime = SimpleNamespace(
        wait_for_extension=AsyncMock(return_value=False),
        open_pairing_page=AsyncMock(return_value=pairing_opened),
    )
    get_or_start = AsyncMock(return_value=runtime)
    resolve_browser = AsyncMock()
    monkeypatch.setenv("BROWSER_TYPE", "extension-connect")
    monkeypatch.setattr(mcp_session.BrowserExtensionRuntime, "get_or_start", get_or_start)
    monkeypatch.setattr(mcp_session, "resolve_browser", resolve_browser)

    result = await mcp_session.skyvern_browser_session_create()

    assert result["ok"] is False
    assert result["error"]["message"] == expected_guidance
    assert "extension-token" not in result["error"]["message"]
    assert "paste the token" not in result["error"]["message"]
    get_or_start.assert_awaited_once_with()
    runtime.open_pairing_page.assert_awaited_once_with()
    runtime.wait_for_extension.assert_awaited_once_with(10.0)
    resolve_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_create_extension_connected_returns_safe_success_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        cdp_ws_url="ws://127.0.0.1/private-capability",
        pairing_token="private-pairing-token",
        wait_for_extension=AsyncMock(return_value=True),
    )
    monkeypatch.setenv("BROWSER_TYPE", "extension-connect")
    monkeypatch.setattr(mcp_session.BrowserExtensionRuntime, "get_or_start", AsyncMock(return_value=runtime))
    monkeypatch.setattr(
        mcp_session,
        "resolve_browser",
        AsyncMock(return_value=(MagicMock(), BrowserContext(mode="extension"))),
    )

    result = await mcp_session.skyvern_browser_session_create()

    assert result["ok"] is True
    assert result["browser_context"]["mode"] == "extension"
    assert result["data"]["browser"] == "extension"
    assert result["data"]["session"] == "implicit"
    assert runtime.cdp_ws_url not in repr(result)
    assert runtime.pairing_token not in repr(result)


@pytest.mark.asyncio
async def test_session_create_extension_runtime_error_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "Browser extension relay port 19777 is already in use"
    monkeypatch.setenv("BROWSER_TYPE", "extension-connect")
    monkeypatch.setattr(
        mcp_session.BrowserExtensionRuntime,
        "get_or_start",
        AsyncMock(side_effect=BrowserExtensionError(message)),
    )

    result = await mcp_session.skyvern_browser_session_create()

    assert result["ok"] is False
    assert result["error"]["message"] == message


@pytest.mark.asyncio
async def test_session_create_extension_connection_error_redacts_capability_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.library.skyvern import Skyvern

    capability_token = "fake-secret-capability"
    capability_url = f"ws://127.0.0.1:43210/cdp/{capability_token}"
    connect_over_cdp = AsyncMock(
        side_effect=RuntimeError(f"BrowserType.connect_over_cdp failed while connecting to {capability_url}")
    )
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp))
    skyvern = object.__new__(Skyvern)
    skyvern._get_playwright = AsyncMock(return_value=playwright)
    runtime = SimpleNamespace(cdp_ws_url=capability_url, wait_for_extension=AsyncMock(return_value=True))
    current_token = session_manager._current_session.set(None)
    monkeypatch.setattr(session_manager, "_global_session", None)
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: skyvern)
    monkeypatch.setenv("BROWSER_TYPE", "extension-connect")
    monkeypatch.setattr(mcp_session.BrowserExtensionRuntime, "get_or_start", AsyncMock(return_value=runtime))

    try:
        result = await mcp_session.skyvern_browser_session_create()
    finally:
        session_manager._current_session.reset(current_token)

    assert result["ok"] is False
    assert result["error"]["message"] == _PAIRING_FALLBACK_GUIDANCE
    assert "/cdp/" not in repr(result)
    assert capability_token not in repr(result)
    connect_over_cdp.assert_awaited_once_with(capability_url)


@pytest.mark.asyncio
async def test_resolve_browser_extension_connects_before_cdp(monkeypatch: pytest.MonkeyPatch) -> None:
    current_token = session_manager._current_session.set(None)
    monkeypatch.setattr(session_manager, "_global_session", None)
    runtime = SimpleNamespace()
    browser = MagicMock()
    fake_skyvern = MagicMock()
    fake_skyvern.connect_to_browser_extension = AsyncMock(return_value=browser)
    fake_skyvern.connect_to_browser_over_cdp = AsyncMock()
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: fake_skyvern)

    try:
        resolved_browser, context = await session_manager.resolve_browser(
            cdp_url="ws://cdp.example.test",
            extension_runtime=runtime,
        )
    finally:
        session_manager._current_session.reset(current_token)

    assert resolved_browser is browser
    assert context == BrowserContext(mode="extension", can_access_localhost=True)
    fake_skyvern.connect_to_browser_extension.assert_awaited_once_with(runtime)
    fake_skyvern.connect_to_browser_over_cdp.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_browser_reconnects_disconnected_extension_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_token = session_manager._current_session.set(None)
    monkeypatch.setattr(session_manager, "_global_session", None)
    playwright_browser = SimpleNamespace(is_connected=Mock(side_effect=[True, False]))
    stale_browser = SimpleNamespace(browser=playwright_browser, close=AsyncMock())
    fresh_browser = SimpleNamespace(browser=SimpleNamespace(is_connected=Mock(return_value=True)))
    runtime = SimpleNamespace()
    fake_skyvern = MagicMock()
    fake_skyvern.connect_to_browser_extension = AsyncMock(return_value=fresh_browser)
    monkeypatch.setattr(session_manager, "get_active_api_key", lambda: None)
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: fake_skyvern)
    session_manager.set_current_session(
        session_manager.SessionState(browser=stale_browser, context=BrowserContext(mode="extension"))
    )

    try:
        first_browser, first_context = await session_manager.resolve_browser(extension_runtime=runtime)
        second_browser, second_context = await session_manager.resolve_browser(extension_runtime=runtime)
    finally:
        session_manager._current_session.reset(current_token)

    assert first_browser is stale_browser
    assert first_context.mode == "extension"
    assert second_browser is fresh_browser
    assert second_context == BrowserContext(mode="extension", can_access_localhost=True)
    assert playwright_browser.is_connected.call_count == 2
    stale_browser.close.assert_awaited_once_with()
    fake_skyvern.connect_to_browser_extension.assert_awaited_once_with(runtime)


def test_extension_session_ref_key_is_stable_and_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_manager, "get_active_api_key", lambda: None)
    extension_state = session_manager.SessionState(context=BrowserContext(mode="extension"))
    local_state = session_manager.SessionState(context=BrowserContext(mode="local"))

    first = session_manager._session_ref_key(extension_state)
    second = session_manager._session_ref_key(extension_state)

    assert first == second == (None, "extension", "own-browser", None)
    assert first != session_manager._session_ref_key(local_state)


@pytest.mark.parametrize("has_existing_context", [True, False])
@pytest.mark.asyncio
async def test_connect_to_browser_extension_uses_runtime_cdp_url_and_context_fallback(
    monkeypatch: pytest.MonkeyPatch,
    has_existing_context: bool,
) -> None:
    from skyvern.library.skyvern import Skyvern

    existing_context = MagicMock()
    new_context = MagicMock()
    browser = SimpleNamespace(
        contexts=[existing_context] if has_existing_context else [],
        new_context=AsyncMock(return_value=new_context),
    )
    connect_over_cdp = AsyncMock(return_value=browser)
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp))
    skyvern = object.__new__(Skyvern)
    skyvern._get_playwright = AsyncMock(return_value=playwright)
    runtime = SimpleNamespace(cdp_ws_url="ws://127.0.0.1/private-capability")

    wrapper_module = types.ModuleType("skyvern.library.skyvern_browser")

    class FakeSkyvernBrowser:
        def __init__(self, client: object, browser_context: object, **kwargs: object) -> None:
            self.client = client
            self.browser_context = browser_context
            self.kwargs = kwargs

    wrapper_module.SkyvernBrowser = FakeSkyvernBrowser
    monkeypatch.setitem(sys.modules, "skyvern.library.skyvern_browser", wrapper_module)

    connected = await skyvern.connect_to_browser_extension(runtime)

    connect_over_cdp.assert_awaited_once_with(runtime.cdp_ws_url)
    assert connected.browser_context is (existing_context if has_existing_context else new_context)
    if has_existing_context:
        browser.new_context.assert_not_awaited()
    else:
        browser.new_context.assert_awaited_once_with()


def test_pairing_confirmation_recovery_retries_through_mcp_without_cli_command() -> None:
    extension_dir = BrowserExtensionRuntime.extension_dir()
    confirmation_html = (extension_dir / "pairing_confirm.html").read_text()
    confirmation_js = (extension_dir / "pairing_confirm.js").read_text()

    assert "Retry the browser session request in your MCP client" in confirmation_html
    assert "skyvern browser extension-pair" in confirmation_html
    assert "Start a new pairing link" not in confirmation_html
    assert "copy-command" not in confirmation_html
    assert "expired before approval" in confirmation_js
    assert "skyvern browser extension-pair" not in confirmation_js
    assert "openPairingPage" not in confirmation_js


def test_browser_extension_path_command_prints_real_absolute_directory() -> None:
    result = CliRunner().invoke(browser_app, ["extension-path"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(BrowserExtensionRuntime.extension_dir().resolve())


def test_browser_extension_token_command_creates_but_does_not_print_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_TOKEN", raising=False)
    monkeypatch.setattr(browser_commands.shutil, "which", lambda _name: None)

    result = CliRunner().invoke(browser_app, ["extension-token"])

    token_path = tmp_path / ".skyvern" / "browser_extension_token"
    token = token_path.read_text()
    assert result.exit_code == 0
    assert token_path.is_file()
    assert token not in result.stdout
    assert result.stdout.splitlines()[0].startswith("Pairing token was not printed.")
    assert "SKYVERN_BROWSER_EXTENSION_TOKEN" in result.stdout
    assert "Paste this token into the Skyvern browser extension popup." in result.stdout


def test_browser_extension_token_command_copies_token_when_clipboard_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "pairing-token-clipboard-sentinel"
    run = MagicMock()
    monkeypatch.setattr(browser_commands, "load_or_create_pairing_token", lambda: token)
    monkeypatch.setattr(browser_commands.sys, "platform", "darwin")
    monkeypatch.setattr(browser_commands.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(browser_commands.subprocess, "run", run)

    result = CliRunner().invoke(browser_app, ["extension-token"])

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "Pairing token copied to clipboard.",
        "Paste this token into the Skyvern browser extension popup.",
        "Click Connect.",
    ]
    run.assert_called_once_with(
        ["/usr/bin/pbcopy"],
        input=token,
        text=True,
        check=True,
        stdout=browser_commands.subprocess.DEVNULL,
        stderr=browser_commands.subprocess.DEVNULL,
    )


def test_browser_extension_status_reports_configuration_without_printing_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = "status-must-not-print-this-token"
    token_dir = tmp_path / ".skyvern"
    token_dir.mkdir()
    token_path = token_dir / "browser_extension_token"
    token_path.write_text(token)
    token_path.chmod(0o600)
    probe = MagicMock(return_value="broker")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_PORT", "20123")
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_TOKEN", raising=False)
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_BROKER", raising=False)
    monkeypatch.setattr(browser_commands, "_bridge_mode", probe)

    result = CliRunner().invoke(browser_app, ["extension-status"])

    assert result.exit_code == 0
    assert str(BrowserExtensionRuntime.extension_dir().resolve()) in result.stdout
    assert "pairing token: configured (file exists)" in result.stdout
    assert "pairing token file permissions: OK" in result.stdout
    assert "shared broker: enabled" in result.stdout
    assert "bridge listening on 20123 (shared broker daemon)" in result.stdout
    assert token not in result.stdout
    probe.assert_called_once_with(20123)


def test_browser_extension_status_flags_a_single_owner_bridge_as_unshareable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_PORT", "20124")
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_TOKEN", raising=False)
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_BROKER", raising=False)
    monkeypatch.setattr(browser_commands, "_bridge_mode", MagicMock(return_value="legacy"))

    result = CliRunner().invoke(browser_app, ["extension-status"])

    assert result.exit_code == 0
    assert "bridge listening on 20124 (single-owner session)" in result.stdout
    assert "share the bridge" in result.stdout


def test_browser_extension_status_is_informational_when_bridge_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_TOKEN", raising=False)
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_PORT", raising=False)
    monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_BROKER", "0")
    monkeypatch.setattr(browser_commands, "_bridge_mode", MagicMock(return_value="none"))

    result = CliRunner().invoke(browser_app, ["extension-status"])

    assert result.exit_code == 0
    assert "pairing token: not configured" in result.stdout
    assert "shared broker: disabled (SKYVERN_BROWSER_EXTENSION_BROKER)" in result.stdout
    assert "bridge not running (start your MCP server with --browser-extension)" in result.stdout


def test_browser_extension_pair_exits_with_guidance_when_bridge_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_commands.socket, "create_connection", MagicMock(side_effect=OSError))

    result = CliRunner().invoke(browser_app, ["extension-pair"])

    assert result.exit_code == 1
    assert "Start your MCP server first: skyvern run mcp --browser-extension" in result.stdout


def test_browser_extension_pair_opens_fragment_url_without_printing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "pair-command-token-must-stay-private"
    open_pairing_url = MagicMock(return_value=True)
    monkeypatch.setattr(browser_commands.BrowserExtensionRuntime, "configured_port", lambda: 20123)
    monkeypatch.setattr(browser_commands, "_bridge_is_listening", lambda _port: True)
    monkeypatch.setattr(browser_commands, "load_or_create_pairing_token", lambda: token)
    monkeypatch.setattr(browser_commands, "_request_pairing_nonce", lambda _port, _token: "pairing-nonce")
    monkeypatch.setattr(browser_commands, "_open_pairing_url", open_pairing_url)

    result = CliRunner().invoke(browser_app, ["extension-pair"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "Approve the pairing in your browser."
    assert token not in result.stdout
    open_pairing_url.assert_called_once_with("http://127.0.0.1:20123/pair#pairing-nonce")


def test_browser_extension_pair_begin_uses_hex_hmac_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "pair-begin-proof-token"
    response = SimpleNamespace(status=200, read=MagicMock(return_value=b'{"v":1,"nonce":"pairing-nonce"}'))
    connection = MagicMock()
    connection.getresponse.return_value = response
    http_connection = MagicMock(return_value=connection)
    monkeypatch.setattr(browser_commands.http.client, "HTTPConnection", http_connection)

    nonce = browser_commands._request_pairing_nonce(20123, token)

    assert nonce == "pairing-nonce"
    http_connection.assert_called_once_with("127.0.0.1", 20123, timeout=2.0)
    request = connection.request.call_args
    payload = json.loads(request.kwargs["body"])
    expected_proof = hmac.new(token.encode(), b"skyvern-pair-begin-v1", hashlib.sha256).hexdigest()
    assert payload == {"v": 1, "proof": expected_proof}
    connection.close.assert_called_once_with()


def test_browser_extension_install_copies_token_opens_chrome_and_prints_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "install-token-must-stay-private"
    extension_dir = BrowserExtensionRuntime.extension_dir().resolve()
    run = MagicMock()
    monkeypatch.setattr(browser_commands, "load_or_create_pairing_token", lambda: token)
    monkeypatch.setattr(browser_commands.sys, "platform", "darwin")
    monkeypatch.setattr(browser_commands.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(browser_commands.subprocess, "run", run)
    monkeypatch.setattr(browser_commands, "_bridge_is_listening", lambda _port: False)

    result = CliRunner().invoke(browser_app, ["extension-install"])

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == str(extension_dir)
    assert "Pairing token copied to clipboard." in result.stdout
    assert token not in result.stdout
    assert "1. Enable Developer mode." in result.stdout
    assert "2. Click Load unpacked." in result.stdout
    assert f"3. Select {extension_dir}." in result.stdout
    assert "4. Open the Skyvern Agent popup." in result.stdout
    assert "5. Paste the pairing token and click Connect." in result.stdout
    assert '6. Add tabs to the "Skyvern Controlled" group.' in result.stdout
    assert "When your MCP server is running, pair with: skyvern browser extension-pair" in result.stdout
    assert run.call_args_list == [
        call(
            ["/usr/bin/pbcopy"],
            input=token,
            text=True,
            check=True,
            stdout=browser_commands.subprocess.DEVNULL,
            stderr=browser_commands.subprocess.DEVNULL,
        ),
        call(
            ["/usr/bin/open", "-a", "Google Chrome", "chrome://extensions"],
            check=True,
            stdout=browser_commands.subprocess.DEVNULL,
            stderr=browser_commands.subprocess.DEVNULL,
        ),
    ]


def test_browser_extension_install_attempts_one_click_pairing_when_bridge_is_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_pairing = MagicMock()
    load_token = MagicMock()
    monkeypatch.setattr(browser_commands.BrowserExtensionRuntime, "configured_port", lambda: 20123)
    monkeypatch.setattr(browser_commands, "_bridge_is_listening", lambda _port: True)
    monkeypatch.setattr(browser_commands, "_open_chrome_extensions", lambda: False)
    monkeypatch.setattr(browser_commands, "_launch_extension_pairing", launch_pairing)
    monkeypatch.setattr(browser_commands, "load_or_create_pairing_token", load_token)

    result = CliRunner().invoke(browser_app, ["extension-install"])

    assert result.exit_code == 0
    assert "4. Click Approve in the pairing page." in result.stdout
    assert "5. Approve the pairing in the Skyvern Agent confirmation tab." in result.stdout
    assert "pairing token" not in result.stdout.lower()
    launch_pairing.assert_called_once_with(20123)
    load_token.assert_not_called()


def test_run_mcp_browser_extension_flag_starts_and_stops_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    runtime = SimpleNamespace(shutdown=AsyncMock(side_effect=lambda: events.append("shutdown")))

    async def get_or_start() -> object:
        events.append("start")
        return runtime

    async def run_async(**_kwargs: object) -> None:
        assert run_commands.os.environ["BROWSER_TYPE"] == "extension-connect"
        events.append("serve")
        await asyncio.sleep(0)

    async def cleanup() -> None:
        events.append("cleanup")

    # delenv on an absent var records no undo, so run_mcp's os.environ.setdefault
    # would leak into later tests; setenv first guarantees restoration.
    monkeypatch.setenv("BROWSER_TYPE", "leak-guard-sentinel")
    monkeypatch.delenv("BROWSER_TYPE", raising=False)
    monkeypatch.setattr(run_commands, "prepare_cli_runtime", lambda **_kwargs: None)
    monkeypatch.setattr(run_commands.atexit, "register", lambda _callback: None)
    monkeypatch.setattr(run_commands.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(run_commands, "_start_stdin_eof_watcher", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(
        "skyvern.library.local_browser_profile.sweep_local_browser_profiles_with_budget",
        lambda: None,
    )
    monkeypatch.setattr("skyvern.cli.mcp_tools.mcp.run_async", run_async)
    monkeypatch.setattr(BrowserExtensionRuntime, "get_or_start", AsyncMock(side_effect=get_or_start))
    monkeypatch.setattr(BrowserExtensionRuntime, "instance", MagicMock(return_value=runtime))
    monkeypatch.setattr(run_commands, "_mcp_cleanup_done", False)
    monkeypatch.setattr(run_commands, "_mcp_cleanup_in_progress", False)

    run_commands.run_mcp(browser_extension=True)

    assert run_commands.os.environ["BROWSER_TYPE"] == "extension-connect"
    # Serving precedes the bridge: `initialize` must not wait on relay startup.
    assert events == ["serve", "start", "cleanup", "shutdown"]
    runtime.shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_run_mcp_with_cleanup_serves_when_extension_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "browser extension bridge unavailable"
    get_or_start = AsyncMock(side_effect=BrowserExtensionError(message))
    served = MagicMock()

    async def run_async(**kwargs: object) -> None:
        served(**kwargs)
        await asyncio.sleep(0)

    cleanup = AsyncMock()
    warning = MagicMock()
    instance = MagicMock(return_value=None)
    monkeypatch.setattr(BrowserExtensionRuntime, "get_or_start", get_or_start)
    monkeypatch.setattr(BrowserExtensionRuntime, "instance", instance)
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(run_commands.LOG, "warning", warning)

    await run_commands._run_mcp_with_cleanup(run_async, browser_extension=True, transport="stdio")

    get_or_start.assert_awaited_once_with()
    served.assert_called_once_with(transport="stdio")
    cleanup.assert_awaited_once_with()
    instance.assert_called_once_with()
    assert warning.call_count == 1
    assert warning.call_args.kwargs["error"] == message


@pytest.mark.asyncio
async def test_run_mcp_with_cleanup_serves_on_unexpected_extension_start_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_or_start = AsyncMock(side_effect=RuntimeError("unexpected startup failure"))
    cleanup = AsyncMock()
    warning = MagicMock()
    served = MagicMock()

    async def run_async(**_kwargs: object) -> None:
        served()
        await asyncio.sleep(0)

    monkeypatch.setattr(BrowserExtensionRuntime, "get_or_start", get_or_start)
    monkeypatch.setattr(BrowserExtensionRuntime, "instance", MagicMock(return_value=None))
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)
    monkeypatch.setattr(run_commands.LOG, "warning", warning)

    await run_commands._run_mcp_with_cleanup(run_async, browser_extension=True)

    get_or_start.assert_awaited_once_with()
    served.assert_called_once_with()
    cleanup.assert_awaited_once_with()
    assert warning.call_count == 1
    assert warning.call_args.kwargs["exc_info"] is True


@pytest.mark.asyncio
async def test_run_mcp_with_cleanup_serves_while_extension_start_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def get_or_start() -> object:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("bridge startup must stay pending")

    async def run_async(**_kwargs: object) -> None:
        await asyncio.wait_for(started.wait(), timeout=1)

    cleanup = AsyncMock()
    instance = MagicMock(return_value=None)
    monkeypatch.setattr(BrowserExtensionRuntime, "get_or_start", AsyncMock(side_effect=get_or_start))
    monkeypatch.setattr(BrowserExtensionRuntime, "instance", instance)
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources", cleanup)

    await run_commands._run_mcp_with_cleanup(run_async, browser_extension=True)

    cleanup.assert_awaited_once_with()
    instance.assert_called_once_with()
    assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
def test_run_mcp_rejects_browser_extension_with_http_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    monkeypatch.setenv("BROWSER_TYPE", "leak-guard-sentinel")
    monkeypatch.delenv("BROWSER_TYPE", raising=False)

    with pytest.raises(typer.BadParameter, match="--browser-extension requires --transport stdio"):
        run_commands.run_mcp(transport=transport, browser_extension=True)

    assert "BROWSER_TYPE" not in run_commands.os.environ
