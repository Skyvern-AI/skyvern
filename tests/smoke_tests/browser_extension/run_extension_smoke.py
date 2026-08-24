from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from fastmcp import Client
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from skyvern.browser_extension.broker_client import BrokerClient
from skyvern.browser_extension.broker_state import broker_paths, read_extension_secret
from skyvern.browser_extension.protocol import EXTENSION_ID
from skyvern.browser_extension.runtime import BrowserExtensionRuntime
from tests.evals.mcp.task import build_mcp_stdio_transport, unwrap_tool_result

EXPECTED_EXTENSION_ID = EXTENSION_ID
TOKEN_ENV = "SKYVERN_BROWSER_EXTENSION_TOKEN"
PORT_ENV = "SKYVERN_BROWSER_EXTENSION_PORT"
BROKER_ENV = "SKYVERN_BROWSER_EXTENSION_BROKER"
SMOKE_TEXT = "Skyvern extension bridge smoke"
CHECKS = (
    "setup",
    "extension paired",
    "navigate",
    "screenshot",
    "trusted selector click",
    "iframe evaluate",
    "new tab",
    "close tab",
    "chrome survives disconnect",
    "mcp session/navigate/observe",
    "mcp coordinate click",
    "mcp coordinate type",
)
CDP_URL_PATTERN = re.compile(r"ws://127\.0\.0\.1:\d+/cdp/[A-Za-z0-9_-]+")


class SmokeReport:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.recorded: set[str] = set()

    def pass_check(self, label: str, details: str | None = None) -> None:
        self.passed += 1
        self.recorded.add(label)
        print(_status_line("PASS", label, details))

    def fail_check(self, label: str, details: str | None = None) -> None:
        self.failed += 1
        self.recorded.add(label)
        print(_status_line("FAIL", label, details))

    def skip_check(self, label: str, details: str | None = None) -> None:
        self.skipped += 1
        self.recorded.add(label)
        print(_status_line("SKIP", label, details))

    def skip_unrecorded(self, reason: str) -> None:
        for label in CHECKS:
            if label not in self.recorded:
                self.skip_check(label, reason)

    def print_summary(self) -> None:
        print()
        print(f"SMOKE RESULT: {self.passed} passed, {self.failed} failed, {self.skipped} skipped")


@dataclass
class FixtureServers:
    outer_runner: web.AppRunner
    iframe_runner: web.AppRunner
    outer_url: str

    async def close(self) -> None:
        try:
            await self.outer_runner.cleanup()
        finally:
            await self.iframe_runner.cleanup()


def _status_line(status: str, label: str, details: str | None) -> str:
    suffix = f": {details}" if details else ""
    return f"{status} {label}{suffix}"


def _reserve_free_port() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
    except BaseException:
        listener.close()
        raise
    return listener, int(listener.getsockname()[1])


def _extension_id_from_key(manifest_key: str) -> str:
    public_key = base64.b64decode(manifest_key, validate=True)
    digest_prefix = hashlib.sha256(public_key).hexdigest()[:32]
    return "".join(chr(ord("a") + int(nibble, 16)) for nibble in digest_prefix)


def _extension_id_from_manifest(extension_dir: Path) -> str:
    manifest_value = json.loads((extension_dir / "manifest.json").read_text())
    if not isinstance(manifest_value, dict):
        raise ValueError("extension manifest must contain a JSON object")
    manifest_key = manifest_value.get("key")
    if not isinstance(manifest_key, str) or not manifest_key:
        raise ValueError("extension manifest is missing its key")
    return _extension_id_from_key(manifest_key)


def _safe_text(value: object, redactions: Sequence[str]) -> str:
    text = CDP_URL_PATTERN.sub("<redacted-cdp-url>", str(value))
    for secret_value in redactions:
        if secret_value:
            text = text.replace(secret_value, "<redacted>")
    text = " ".join(text.split())
    return text if len(text) <= 600 else f"{text[:300]} ... {text[-295:]}"


async def _discover_extension_id(context: BrowserContext, timeout_seconds: float = 15.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        parsed_urls = (urlsplit(worker.url) for worker in context.service_workers)
        extension_ids = {
            parsed.netloc for parsed in parsed_urls if parsed.scheme == "chrome-extension" and parsed.netloc
        }
        if EXPECTED_EXTENSION_ID in extension_ids:
            return EXPECTED_EXTENSION_ID
        if extension_ids:
            return sorted(extension_ids)[0]
        await asyncio.sleep(0.1)
    raise TimeoutError("no chrome-extension service worker appeared within 15 seconds")


async def _wait_for_pairing(runtime: BrowserExtensionRuntime, timeout_seconds: float = 15.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if runtime.extension_connected:
            return True
        await asyncio.sleep(0.1)
    return runtime.extension_connected


def _install_pairing_url_capture(directory: Path) -> Path:
    capture_path = directory / "pairing-urls.log"
    script = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"with Path({str(capture_path)!r}).open('a') as output:\n"
        "    output.write(sys.argv[-1] + '\\n')\n"
    )
    for executable_name in ("open", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        executable = directory / executable_name
        executable.write_text(script, encoding="utf-8")
        executable.chmod(0o700)
    return capture_path


def _pairing_url_count(capture_path: Path) -> int:
    if not capture_path.exists():
        return 0
    return len([line for line in capture_path.read_text(encoding="utf-8").splitlines() if line])


def _assert_one_pairing_url(capture_path: Path, previous_count: int) -> None:
    current_count = _pairing_url_count(capture_path)
    if current_count != previous_count + 1:
        raise AssertionError(f"expected one pairing URL for this client, captured {current_count - previous_count}")


async def _read_next_pairing_url(capture_path: Path, previous_count: int, timeout_seconds: float = 20.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if capture_path.exists():
            urls = [line for line in capture_path.read_text(encoding="utf-8").splitlines() if line]
            if len(urls) > previous_count:
                return urls[previous_count]
        await asyncio.sleep(0.1)
    raise TimeoutError("broker did not open a pairing URL within 20 seconds")


async def _approve_next_pairing(
    context: BrowserContext,
    capture_path: Path,
    previous_count: int,
    timeout_seconds: float = 20.0,
) -> None:
    pairing_url = await _read_next_pairing_url(capture_path, previous_count, timeout_seconds)
    existing_confirmations = {page for page in context.pages if "pairing_confirm.html" in page.url}
    pairing_page = await context.new_page()
    await pairing_page.goto(pairing_url, wait_until="domcontentloaded")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    confirmation: Page | None = None
    while loop.time() < deadline:
        confirmation = next(
            (
                page
                for page in context.pages
                if page not in existing_confirmations and "pairing_confirm.html" in page.url and not page.is_closed()
            ),
            None,
        )
        if confirmation is not None and await confirmation.locator("#approve-button").is_enabled():
            break
        await asyncio.sleep(0.1)
    else:
        raise TimeoutError("extension did not show an enabled pairing approval within 20 seconds")
    assert confirmation is not None

    await confirmation.locator("#approve-button").click()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if await confirmation.locator("body").get_attribute("data-state") == "success":
            await pairing_page.close()
            await confirmation.close()
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("extension did not confirm pairing approval within 20 seconds")


async def _start_runner(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    try:
        await site.start()
    except BaseException:
        await runner.cleanup()
        raise
    addresses = runner.addresses
    if not addresses:
        await runner.cleanup()
        raise RuntimeError("fixture server failed to bind")
    return runner, int(addresses[0][1])


async def _start_fixture_servers() -> FixtureServers:
    async def iframe_handler(_request: web.Request) -> web.Response:
        return web.Response(
            text='<!doctype html><html><body><div id="frame-marker">iframe-ready</div></body></html>',
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    iframe_runner, iframe_port = await _start_runner(iframe_handler)
    iframe_url = f"http://127.0.0.1:{iframe_port}/"

    async def outer_handler(_request: web.Request) -> web.Response:
        html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Extension Bridge Fixture</title><style>
#smoke-button {{ position: fixed; left: 40px; top: 40px; width: 160px; height: 40px; }}
#text-input {{ position: fixed; left: 40px; top: 120px; width: 240px; height: 32px; }}
#click-result {{ position: fixed; left: 40px; top: 180px; }}
#password-input {{ position: fixed; left: 40px; top: 220px; }}
#smoke-frame {{ position: fixed; left: 320px; top: 40px; width: 320px; height: 180px; }}
</style></head><body>
<button id="smoke-button" type="button">Smoke button</button>
<input id="text-input" type="text"><input id="password-input" type="password">
<div id="click-result">not-clicked</div>
<iframe id="smoke-frame" src="{iframe_url}"></iframe><script>
window.smokeState = {{clickCount: 0, lastTrusted: false}};
document.querySelector("#smoke-button").addEventListener("click", (event) => {{
  window.smokeState.clickCount += 1; window.smokeState.lastTrusted = event.isTrusted;
  document.querySelector("#click-result").textContent = `clicked:${{window.smokeState.clickCount}}`;
}});</script></body></html>"""
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    try:
        outer_runner, outer_port = await _start_runner(outer_handler)
    except BaseException:
        await iframe_runner.cleanup()
        raise
    return FixtureServers(
        outer_runner=outer_runner,
        iframe_runner=iframe_runner,
        outer_url=f"http://127.0.0.1:{outer_port}/",
    )


def _tool_error(result: dict[str, object]) -> str:
    error = result.get("error")
    if not isinstance(error, dict):
        return "tool returned ok=false"
    code = error.get("code")
    message = error.get("message")
    return f"{code}: {message}"


async def _wait_for_popup_connection(popup_page: Page, timeout_seconds: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        status = (await popup_page.locator("#status-label").text_content() or "").strip()
        if status in {"Connected", "Client attached"}:
            return
        await asyncio.sleep(0.1)
    error = await popup_page.locator("#connection-error").text_content()
    raise TimeoutError(f"extension did not reconnect to MCP relay; popup error: {error or 'none'}")


async def _wait_for_fixture_page(context: BrowserContext, fixture_url: str, timeout_seconds: float = 15.0) -> Page:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        matches = [page for page in context.pages if not page.is_closed() and page.url == fixture_url]
        if matches:
            return matches[-1]
        await asyncio.sleep(0.1)
    raise TimeoutError(f"MCP navigation did not expose fixture page {fixture_url}")


async def _run_mcp_phase(
    context: BrowserContext,
    popup_page: Page,
    fixture_url: str,
    relay_port: int,
    pairing_token: str,
    legacy_mode: bool,
    pairing_url_capture: Path,
    report: SmokeReport,
) -> None:
    env_overrides = {
        PORT_ENV: str(relay_port),
        # Keep the production SSRF default intact; only this disposable child may reach its loopback fixture.
        "ALLOWED_HOSTS": json.dumps(["127.0.0.1"]),
    }
    if legacy_mode:
        env_overrides[TOKEN_ENV] = pairing_token
    transport = build_mcp_stdio_transport(
        browser_extension=True,
        env_overrides=env_overrides,
    )
    session_created = False
    session_create_started = False
    async with Client(transport, timeout=60) as client:
        await _wait_for_popup_connection(popup_page)

        async def call_tool(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
            return unwrap_tool_result(await client.call_tool_mcp(name, arguments or {}))

        try:
            session_create_started = True
            if legacy_mode:
                session_result = await call_tool("skyvern_browser_session_create")
            else:
                previous_count = _pairing_url_count(pairing_url_capture)
                session_result = await call_tool("skyvern_browser_session_create")
                if _pairing_url_count(pairing_url_capture) != previous_count:
                    raise AssertionError("workstation grant unexpectedly opened a pairing URL")
            if session_result.get("ok") is not True:
                raise AssertionError(f"session create failed: {_tool_error(session_result)}")
            session_created = True
            assert pairing_token not in repr(session_result)
            assert CDP_URL_PATTERN.search(repr(session_result)) is None

            navigate_result = await call_tool("skyvern_navigate", {"url": fixture_url})
            observe_result = await call_tool("skyvern_observe", {"selector": "body", "interactive_only": True})
            statuses = (
                f"session={'PASS' if session_result.get('ok') is True else 'FAIL'} "
                f"navigate={'PASS' if navigate_result.get('ok') is True else 'FAIL'} "
                f"observe={'PASS' if observe_result.get('ok') is True else 'FAIL'}"
            )
            if navigate_result.get("ok") is not True:
                raise AssertionError(f"{statuses}; navigate error={_tool_error(navigate_result)}")
            if observe_result.get("ok") is not True:
                raise AssertionError(f"{statuses}; observe error={_tool_error(observe_result)}")
            report.pass_check("mcp session/navigate/observe", statuses)

            fixture_page = await _wait_for_fixture_page(context, fixture_url)
            button_bounds = await fixture_page.locator("#smoke-button").bounding_box()
            assert button_bounds is not None, "MCP fixture button has no bounding box"
            click_x = button_bounds["x"] + button_bounds["width"] / 2
            click_y = button_bounds["y"] + button_bounds["height"] / 2
            click_result = await call_tool("skyvern_click", {"x": click_x, "y": click_y})
            click_data = click_result.get("data")
            assert click_result.get("ok") is True, _tool_error(click_result)
            assert isinstance(click_data, dict)
            assert click_data.get("x") == click_x
            assert click_data.get("y") == click_y
            assert click_data.get("resolved_target")
            assert await fixture_page.locator("#click-result").text_content() == "clicked:1"
            report.pass_check("mcp coordinate click", f"x={click_x:.1f}, y={click_y:.1f}")

            input_bounds = await fixture_page.locator("#text-input").bounding_box()
            assert input_bounds is not None, "MCP fixture input has no bounding box"
            type_x = input_bounds["x"] + input_bounds["width"] / 2
            type_y = input_bounds["y"] + input_bounds["height"] / 2
            type_result = await call_tool("skyvern_type", {"x": type_x, "y": type_y, "text": SMOKE_TEXT})
            type_data = type_result.get("data")
            assert type_result.get("ok") is True, _tool_error(type_result)
            assert isinstance(type_data, dict)
            assert type_data.get("x") == type_x
            assert type_data.get("y") == type_y
            assert type_data.get("resolved_target")
            assert await fixture_page.locator("#text-input").input_value() == SMOKE_TEXT
            report.pass_check("mcp coordinate type", f"x={type_x:.1f}, y={type_y:.1f}")
        finally:
            if session_create_started:
                try:
                    close_result = await call_tool("skyvern_browser_session_close")
                except Exception:
                    if session_created:
                        raise
                else:
                    if session_created:
                        assert close_result.get("ok") is True, _tool_error(close_result)


async def _pair_extension(
    context: BrowserContext,
    runtime: BrowserExtensionRuntime,
    extension_id: str,
    relay_port: int,
    pairing_token: str,
    legacy_mode: bool,
    pairing_url_capture: Path,
) -> Page:
    popup_page = await context.new_page()
    await popup_page.goto(f"chrome-extension://{extension_id}/popup.html", wait_until="domcontentloaded")
    await popup_page.locator("#advanced-settings summary").click()
    await popup_page.locator("#bridge-port").fill(str(relay_port))
    await popup_page.locator("#pairing-token").fill(pairing_token)
    await popup_page.locator("#connection-button").click()
    if not legacy_mode:
        previous_count = _pairing_url_count(pairing_url_capture)
        if not await runtime.begin_pairing():
            raise RuntimeError("broker did not start this client's pairing flow")
        await _approve_next_pairing(context, pairing_url_capture, previous_count)
    if not await _wait_for_pairing(runtime):
        popup_error = await popup_page.locator("#connection-error").text_content()
        raise TimeoutError(f"extension did not pair within 15 seconds; popup error: {popup_error or 'none'}")
    if not legacy_mode:
        _assert_one_pairing_url(pairing_url_capture, previous_count)
    return popup_page


async def _exercise_page(page: Page, report: SmokeReport, screenshot_path: Path) -> FixtureServers:
    servers = await _start_fixture_servers()
    try:
        await page.goto(servers.outer_url, wait_until="domcontentloaded")
        report.pass_check("navigate", "loopback fixture")

        await page.screenshot(path=screenshot_path)
        assert screenshot_path.is_file() and screenshot_path.stat().st_size > 0
        report.pass_check("screenshot", str(screenshot_path))

        await page.goto(servers.outer_url, wait_until="domcontentloaded")
        await page.click("#smoke-button")
        selector_state = await page.evaluate("() => ({...window.smokeState})")
        assert isinstance(selector_state, dict)
        assert selector_state.get("clickCount") == 1
        assert selector_state.get("lastTrusted") is True
        report.pass_check("trusted selector click")

        assert await page.locator("#password-input").count() == 1

        iframe_element = await page.wait_for_selector("#smoke-frame")
        frame = await iframe_element.content_frame()
        assert frame is not None, "iframe did not expose a Playwright frame"
        await frame.wait_for_selector("#frame-marker")
        marker = await frame.evaluate("document.querySelector('#frame-marker')?.textContent")
        outer_origin = await page.evaluate("location.origin")
        iframe_origin = await frame.evaluate("location.origin")
        assert marker == "iframe-ready"
        assert iframe_origin != outer_origin
        report.pass_check("iframe evaluate", f"cross-origin frame={iframe_origin}")
    except BaseException:
        await servers.close()
        raise
    return servers


async def _cleanup(
    bridge_browser: Browser | None,
    fixture_servers: FixtureServers | None,
    persistent_context: BrowserContext | None,
    runtime: BrowserExtensionRuntime | None,
    playwright: Playwright | None,
    profile_dir: tempfile.TemporaryDirectory[str] | None,
    evidence_dir: tempfile.TemporaryDirectory[str] | None,
    broker_port: int | None,
) -> list[str]:
    errors: list[str] = []
    cleanup_steps: list[tuple[str, Callable[[], Awaitable[object]]]] = []
    if bridge_browser is not None and bridge_browser.is_connected():
        cleanup_steps.append(("bridge browser", bridge_browser.close))
    if fixture_servers is not None:
        cleanup_steps.append(("fixture servers", fixture_servers.close))
    if persistent_context is not None:
        cleanup_steps.append(("persistent context", persistent_context.close))
    if runtime is not None:
        cleanup_steps.append(("extension runtime", runtime.shutdown))
    if playwright is not None:
        cleanup_steps.append(("playwright", playwright.stop))
    if broker_port is not None:
        cleanup_steps.append(("broker daemon", lambda: _stop_broker(broker_port)))
    for label, cleanup_step in cleanup_steps:
        try:
            await cleanup_step()
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    for label, temp_dir in (("temporary profile", profile_dir), ("temporary evidence", evidence_dir)):
        if temp_dir is not None:
            try:
                temp_dir.cleanup()
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
    return errors


async def _stop_broker(port: int) -> None:
    async def ignore_event(_event: str, _params: dict) -> None:
        return None

    client = BrokerClient(port, ignore_event, auto_spawn=False, operator=True)
    try:
        await client.start()
        await client.stop_broker()
    finally:
        await client.stop()


async def run_smoke(*, legacy_mode: bool) -> int:
    report = SmokeReport()
    pairing_token = secrets.token_urlsafe(32) if legacy_mode else None
    previous_token = os.environ.get(TOKEN_ENV)
    if legacy_mode:
        assert pairing_token is not None
        os.environ[TOKEN_ENV] = pairing_token
    else:
        os.environ.pop(TOKEN_ENV, None)
    redactions = [pairing_token] if pairing_token is not None else []
    previous_path = os.environ.get("PATH")
    runtime: BrowserExtensionRuntime | None = None
    playwright: Playwright | None = None
    persistent_context: BrowserContext | None = None
    bridge_browser: Browser | None = None
    fixture_servers: FixtureServers | None = None
    profile_dir: tempfile.TemporaryDirectory[str] | None = None
    opener_dir = tempfile.TemporaryDirectory(prefix="skyvern-extension-smoke-opener-")
    pairing_url_capture = _install_pairing_url_capture(Path(opener_dir.name))
    os.environ["PATH"] = f"{opener_dir.name}:{previous_path or '/usr/bin:/bin'}"
    evidence_dir = tempfile.TemporaryDirectory(prefix="skyvern-extension-smoke-evidence-")
    screenshot_path = (Path(evidence_dir.name) / "smoke_navigate.png").resolve()
    broker_port: int | None = None

    try:
        if legacy_mode and os.environ.get(BROKER_ENV) != "0":
            raise RuntimeError(f"legacy smoke requires {BROKER_ENV}=0")
        if not legacy_mode and BROKER_ENV in os.environ:
            raise RuntimeError(f"broker-default smoke requires {BROKER_ENV} to be unset")
        port_reservation, relay_port = _reserve_free_port()
        port_reservation.close()
        runtime = await BrowserExtensionRuntime.get_or_start(port=relay_port)
        if legacy_mode:
            assert pairing_token is not None
        else:
            broker_port = relay_port
            assert TOKEN_ENV not in os.environ
            pairing_token = read_extension_secret(broker_paths(relay_port))
            redactions.append(pairing_token)
        redactions.append(runtime.cdp_ws_url)
        extension_dir = BrowserExtensionRuntime.extension_dir().resolve()
        derived_extension_id = _extension_id_from_manifest(extension_dir)
        assert derived_extension_id == EXPECTED_EXTENSION_ID, (
            f"manifest key derived {derived_extension_id}, expected {EXPECTED_EXTENSION_ID}"
        )

        playwright = await async_playwright().start()
        profile_dir = tempfile.TemporaryDirectory(prefix="skyvern-extension-smoke-profile-")
        extension_path = str(extension_dir)
        persistent_context = await playwright.chromium.launch_persistent_context(
            profile_dir.name,
            channel="chromium",
            headless=False,
            args=[
                f"--disable-extensions-except={extension_path}",
                f"--load-extension={extension_path}",
                "--use-mock-keychain",
                "--password-store=basic",
            ],
        )
        discovered_extension_id = await _discover_extension_id(persistent_context)
        if discovered_extension_id != EXPECTED_EXTENSION_ID:
            print(f"WARN extension ID mismatch: expected {EXPECTED_EXTENSION_ID}, discovered {discovered_extension_id}")
        assert discovered_extension_id == EXPECTED_EXTENSION_ID
        extension_id = discovered_extension_id
        mode = "legacy opt-out" if legacy_mode else "broker default"
        report.pass_check("setup", f"mode={mode}, relay port={relay_port}, extension ID={extension_id}")

        popup_page = await _pair_extension(
            persistent_context,
            runtime,
            extension_id,
            relay_port,
            pairing_token,
            legacy_mode,
            pairing_url_capture,
        )
        assert runtime.extension_connected
        report.pass_check("extension paired")

        bridge_browser = await playwright.chromium.connect_over_cdp(runtime.cdp_ws_url)
        assert bridge_browser.contexts, "bridge connection exposed no browser context"
        bridge_context = bridge_browser.contexts[0]
        bridge_page = bridge_context.pages[0] if bridge_context.pages else await bridge_context.new_page()
        fixture_servers = await _exercise_page(bridge_page, report, screenshot_path)

        page_count = len(bridge_context.pages)
        new_page = await bridge_context.new_page()
        assert len(bridge_context.pages) == page_count + 1
        report.pass_check("new tab")
        await new_page.close()
        assert new_page.is_closed()
        assert len(bridge_context.pages) == page_count
        report.pass_check("close tab")

        await bridge_browser.close()
        assert not bridge_browser.is_connected()
        assert persistent_context.pages, "persistent Chrome context has no pages after bridge disconnect"
        surviving_page = next((page for page in persistent_context.pages if not page.is_closed()), None)
        assert surviving_page is not None
        assert await surviving_page.evaluate("1 + 1") == 2
        assert runtime.extension_connected
        report.pass_check("chrome survives disconnect")

        await runtime.shutdown()
        runtime = None
        await _run_mcp_phase(
            persistent_context,
            popup_page,
            fixture_servers.outer_url,
            relay_port,
            pairing_token,
            legacy_mode,
            pairing_url_capture,
            report,
        )
    except Exception as exc:
        failed_check = next((label for label in CHECKS if label not in report.recorded), "smoke")
        report.fail_check(failed_check, _safe_text(f"{type(exc).__name__}: {exc}", redactions))
        report.skip_unrecorded(f"blocked by {failed_check}")
    finally:
        cleanup_errors = await _cleanup(
            bridge_browser,
            fixture_servers,
            persistent_context,
            runtime,
            playwright,
            profile_dir,
            evidence_dir,
            broker_port,
        )
        if cleanup_errors:
            report.fail_check("cleanup", _safe_text("; ".join(cleanup_errors), redactions))
        if previous_token is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous_token
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path
        try:
            opener_dir.cleanup()
        except Exception as exc:
            report.fail_check("cleanup", _safe_text(f"temporary opener: {type(exc).__name__}: {exc}", redactions))

    report.print_summary()
    return 1 if report.failed else 0


def _dry_run_helpers() -> int:
    extension_dir = BrowserExtensionRuntime.extension_dir().resolve()
    derived_extension_id = _extension_id_from_manifest(extension_dir)
    if derived_extension_id != EXPECTED_EXTENSION_ID:
        print(f"HELPER FAIL extension ID: expected {EXPECTED_EXTENSION_ID}, derived {derived_extension_id}")
        return 1
    print(f"HELPER PASS extension ID: {derived_extension_id}")
    port_reservation, free_port = _reserve_free_port()
    port_reservation.close()
    if not 0 < free_port <= 65535:
        print(f"HELPER FAIL free port: {free_port}")
        return 1
    print(f"HELPER PASS free port: {free_port}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the real Chrome extension bridge smoke test")
    parser.add_argument("--dry-run-helpers", action="store_true", help="validate pure-Python helpers only")
    parser.add_argument("--legacy", action="store_true", help="exercise the explicit broker opt-out path")
    args = parser.parse_args(argv)
    if args.dry_run_helpers:
        return _dry_run_helpers()
    return asyncio.run(run_smoke(legacy_mode=args.legacy))


if __name__ == "__main__":
    raise SystemExit(main())
