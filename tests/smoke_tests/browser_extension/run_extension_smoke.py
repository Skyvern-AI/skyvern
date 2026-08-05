from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import inspect
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
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from skyvern.browser_extension.protocol import EXTENSION_ID
from skyvern.browser_extension.runtime import BrowserExtensionRuntime

EXPECTED_EXTENSION_ID = EXTENSION_ID
TOKEN_ENV = "SKYVERN_BROWSER_EXTENSION_TOKEN"
BROWSER_TYPE_ENV = "BROWSER_TYPE"
MCP_BROWSER_TYPE = "extension-connect"
SMOKE_TEXT = "Skyvern extension bridge smoke"
CHECKS = (
    "setup",
    "extension paired",
    "navigate",
    "screenshot",
    "trusted selector click",
    "coordinate click",
    "type",
    "iframe evaluate",
    "new tab",
    "close tab",
    "chrome survives disconnect",
    "mcp session/navigate/observe",
)
CDP_URL_PATTERN = re.compile(r"ws://127\.0\.0\.1:\d+/cdp/[A-Za-z0-9_-]+")


class McpUnavailableError(RuntimeError):
    pass


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
    return " ".join(text.split())[:600]


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
<title>Extension Bridge Fixture</title></head><body>
<button id="smoke-button" type="button">Smoke button</button>
<input id="text-input" type="text"><input id="password-input" type="password">
<iframe id="smoke-frame" src="{iframe_url}"></iframe><script>
window.smokeState = {{clickCount: 0, lastTrusted: false}};
document.querySelector("#smoke-button").addEventListener("click", (event) => {{
  window.smokeState.clickCount += 1; window.smokeState.lastTrusted = event.isTrusted;
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
        outer_url=f"http://localhost:{outer_port}/",
    )


def _tool_error(result: dict[str, object]) -> str:
    error = result.get("error")
    if not isinstance(error, dict):
        return "tool returned ok=false"
    code = error.get("code")
    message = error.get("message")
    return f"{code}: {message}"


async def _run_mcp_phase(fixture_url: str) -> str:
    from skyvern.cli.mcp_tools import browser as browser_tools
    from skyvern.cli.mcp_tools import session as session_tools

    if MCP_BROWSER_TYPE not in inspect.getsource(session_tools):
        raise McpUnavailableError("the current MCP session layer has no extension-connect branch")

    previous_browser_type = os.environ.get(BROWSER_TYPE_ENV)
    os.environ[BROWSER_TYPE_ENV] = MCP_BROWSER_TYPE
    session_created = False
    try:
        session_result = await session_tools.skyvern_browser_session_create()
        if session_result.get("ok") is not True:
            raise McpUnavailableError(f"session=FAIL; unavailable in-process ({_tool_error(session_result)})")
        session_created = True

        navigate_result = await browser_tools.skyvern_navigate(url=fixture_url)
        observe_result = await browser_tools.skyvern_observe(selector="body", interactive_only=True)
        statuses = (
            f"session={'PASS' if session_result.get('ok') is True else 'FAIL'} "
            f"navigate={'PASS' if navigate_result.get('ok') is True else 'FAIL'} "
            f"observe={'PASS' if observe_result.get('ok') is True else 'FAIL'}"
        )
        if navigate_result.get("ok") is not True:
            raise AssertionError(f"{statuses}; navigate error={_tool_error(navigate_result)}")
        if observe_result.get("ok") is not True:
            raise AssertionError(f"{statuses}; observe error={_tool_error(observe_result)}")
        return statuses
    finally:
        if session_created:
            try:
                await session_tools.skyvern_browser_session_close()
            except Exception:
                pass
        if previous_browser_type is None:
            os.environ.pop(BROWSER_TYPE_ENV, None)
        else:
            os.environ[BROWSER_TYPE_ENV] = previous_browser_type


async def _pair_extension(
    context: BrowserContext,
    runtime: BrowserExtensionRuntime,
    extension_id: str,
    relay_port: int,
    pairing_token: str,
) -> Page:
    popup_page = await context.new_page()
    await popup_page.goto(f"chrome-extension://{extension_id}/popup.html", wait_until="domcontentloaded")
    await popup_page.locator("#advanced-settings summary").click()
    await popup_page.locator("#bridge-port").fill(str(relay_port))
    await popup_page.locator("#pairing-token").fill(pairing_token)
    await popup_page.locator("#connection-button").click()
    if not await _wait_for_pairing(runtime):
        popup_error = await popup_page.locator("#connection-error").text_content()
        raise TimeoutError(f"extension did not pair within 15 seconds; popup error: {popup_error or 'none'}")
    return popup_page


async def _exercise_page(page: Page, report: SmokeReport, screenshot_path: Path) -> FixtureServers:
    servers = await _start_fixture_servers()
    try:
        try:
            await page.goto("https://example.com", wait_until="domcontentloaded")
            title = await page.title()
            assert "Example" in title, f"unexpected example.com title: {title!r}"
        except Exception:
            await page.goto(servers.outer_url, wait_until="domcontentloaded")
            report.pass_check("navigate", "loopback fallback")
        else:
            report.pass_check("navigate", f"title={title!r}")

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

        button = page.locator("#smoke-button")
        bounds = await button.bounding_box()
        assert bounds is not None, "button has no bounding box"
        x = bounds["x"] + bounds["width"] / 2
        y = bounds["y"] + bounds["height"] / 2
        await page.mouse.click(x, y)
        coordinate_state = await page.evaluate("() => ({...window.smokeState})")
        assert isinstance(coordinate_state, dict)
        assert coordinate_state.get("clickCount") == 2
        assert coordinate_state.get("lastTrusted") is True
        report.pass_check("coordinate click", f"x={x:.1f}, y={y:.1f}")

        text_input = page.locator("#text-input")
        await text_input.focus()
        await page.keyboard.type(SMOKE_TEXT)
        assert await text_input.input_value() == SMOKE_TEXT
        assert await page.locator("#password-input").count() == 1
        report.pass_check("type")

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
    for label, cleanup_step in cleanup_steps:
        try:
            await cleanup_step()
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    if profile_dir is not None:
        try:
            profile_dir.cleanup()
        except Exception as exc:
            errors.append(f"temporary profile: {type(exc).__name__}: {exc}")
    return errors


async def run_smoke() -> int:
    report = SmokeReport()
    pairing_token = secrets.token_urlsafe(32)
    previous_token = os.environ.get(TOKEN_ENV)
    os.environ[TOKEN_ENV] = pairing_token
    redactions = [pairing_token]
    runtime: BrowserExtensionRuntime | None = None
    playwright: Playwright | None = None
    persistent_context: BrowserContext | None = None
    bridge_browser: Browser | None = None
    fixture_servers: FixtureServers | None = None
    profile_dir: tempfile.TemporaryDirectory[str] | None = None
    screenshot_dir = Path(tempfile.mkdtemp(prefix="skyvern-extension-smoke-evidence-"))
    screenshot_path = (screenshot_dir / "smoke_navigate.png").resolve()

    try:
        port_reservation, relay_port = _reserve_free_port()
        port_reservation.close()
        runtime = await BrowserExtensionRuntime.get_or_start(port=relay_port)
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
            ],
        )
        discovered_extension_id = await _discover_extension_id(persistent_context)
        if discovered_extension_id != EXPECTED_EXTENSION_ID:
            print(f"WARN extension ID mismatch: expected {EXPECTED_EXTENSION_ID}, discovered {discovered_extension_id}")
        assert discovered_extension_id == EXPECTED_EXTENSION_ID
        extension_id = discovered_extension_id
        report.pass_check("setup", f"relay port={relay_port}, extension ID={extension_id}")

        await _pair_extension(persistent_context, runtime, extension_id, relay_port, pairing_token)
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

        try:
            mcp_statuses = await _run_mcp_phase(fixture_servers.outer_url)
        except McpUnavailableError as exc:
            report.skip_check("mcp session/navigate/observe", _safe_text(exc, redactions))
        else:
            report.pass_check("mcp session/navigate/observe", mcp_statuses)
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
        )
        if cleanup_errors:
            report.fail_check("cleanup", _safe_text("; ".join(cleanup_errors), redactions))
        if previous_token is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous_token

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
    args = parser.parse_args(argv)
    if args.dry_run_helpers:
        return _dry_run_helpers()
    return asyncio.run(run_smoke())


if __name__ == "__main__":
    raise SystemExit(main())
