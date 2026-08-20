from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from skyvern.forge.sdk.browser_action_policy import AuthorityState, RuntimeOriginAuthority, canonicalize_origin
from skyvern.forge.sdk.browser_network_egress_monitor import BrowserNetworkEgressMonitor
from tests.unit.browser_effect_approval_test_helpers import run_with_consumed_effect


class _RecordingHandler(BaseHTTPRequestHandler):
    received_paths: list[str] = []

    def do_GET(self) -> None:
        type(self).received_paths.append(self.path)
        if self.path == "/":
            body = b"<html><body><img src='/during'></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/same-origin-redirect":
            self.send_response(302)
            self.send_header("Location", "/redirected")
        else:
            self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _authority(url: str) -> RuntimeOriginAuthority:
    origin = canonicalize_origin(url)
    assert origin is not None
    return RuntimeOriginAuthority(AuthorityState.ESTABLISHED, frozenset({origin}))


@pytest.mark.asyncio
async def test_real_chromium_arbitrates_active_requests_and_contains_passive_egress() -> None:
    allowed_handler = type("AllowedHandler", (_RecordingHandler,), {"received_paths": []})
    blocked_handler = type("BlockedHandler", (_RecordingHandler,), {"received_paths": []})
    servers = [
        ThreadingHTTPServer(("127.0.0.1", 0), allowed_handler),
        ThreadingHTTPServer(("127.0.0.1", 0), blocked_handler),
    ]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    allowed_origin = f"http://127.0.0.1:{servers[0].server_port}"
    blocked_origin = f"http://127.0.0.1:{servers[1].server_port}"

    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
                    pytest.skip(f"Chromium not installed for real-browser egress test: {exc}")
                raise
            context = await browser.new_context(service_workers="block")
            monitor = BrowserNetworkEgressMonitor()
            await monitor.install(context)
            monitor.bind_authority(_authority(allowed_origin))
            page = await context.new_page()
            cdp_session = await context.new_cdp_session(page)
            cdp_decisions: list[bool] = []
            cdp_tasks: set[asyncio.Task[None]] = set()

            async def handle_paused(event: dict[str, Any]) -> None:
                resource_type = event.get("resourceType", "").lower()
                request = event["request"]
                allowed = resource_type not in {"document", "download"} or monitor.authorize_request(
                    method=request["method"],
                    url=request["url"],
                    resource_type=resource_type,
                    frame=page.main_frame,
                )
                if resource_type in {"document", "download"}:
                    cdp_decisions.append(allowed)
                await cdp_session.send(
                    "Fetch.continueRequest" if allowed else "Fetch.failRequest",
                    {"requestId": event["requestId"]}
                    if allowed
                    else {"requestId": event["requestId"], "errorReason": "BlockedByClient"},
                )

            def on_paused(event: dict[str, Any]) -> None:
                task = asyncio.create_task(handle_paused(event))
                cdp_tasks.add(task)
                task.add_done_callback(cdp_tasks.discard)

            cdp_session.on("Fetch.requestPaused", on_paused)
            await cdp_session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            monitor.register_active_request_interceptor(page=page, owner=cdp_session)

            async def exercise(consumed: Any) -> None:
                with monitor.open_causal_epoch(consumed):
                    monitor.arm_initial_effect(consumed, method="GET", url=f"{allowed_origin}/")
                    navigation = await page.goto(f"{allowed_origin}/")
                    assert navigation is not None
                    assert navigation.headers["content-security-policy"] == "connect-src 'none'"
                    await page.evaluate(
                        "urls => { for (const src of urls) { const image = document.createElement('img'); "
                        "image.src = src; document.body.append(image); } }",
                        [f"{allowed_origin}/same-origin-redirect", f"{blocked_origin}/blocked"],
                    )
                    await page.wait_for_timeout(100)
                    with pytest.raises(PlaywrightError):
                        await page.goto(f"{allowed_origin}/second")

                    monitor.unregister_active_request_interceptor(page=page, owner=cdp_session)
                    await cdp_session.send("Fetch.disable")
                    await cdp_session.detach()
                    with pytest.raises(PlaywrightError):
                        await page.goto(f"{allowed_origin}/unmonitored")
                    await page.evaluate(
                        "url => document.body.append(Object.assign(document.createElement('img'), {src: url}))",
                        f"{allowed_origin}/passive-after-detach",
                    )
                    await page.wait_for_timeout(100)

            await run_with_consumed_effect(exercise)
            await page.evaluate(
                "url => document.body.append(Object.assign(document.createElement('img'), {src: url}))",
                f"{allowed_origin}/after",
            )
            websocket_blocked = await page.evaluate(
                "url => { try { new WebSocket(url); return false; } catch { return true; } }",
                f"ws://127.0.0.1:{servers[0].server_port}/socket",
            )
            # Require the init-script override so CSP cannot mask a missing WebTransport deny.
            webtransport_blocked = await page.evaluate(
                "() => { if (typeof WebTransport !== 'function' || WebTransport.name !== 'BlockedNetworkAPI') "
                "return false; try { new WebTransport('https://example.com/'); return false; } "
                "catch (error) { return error.name === 'SecurityError'; } }"
            )
            worker_blocked = await page.evaluate(
                "() => { try { new Worker(URL.createObjectURL(new Blob(['']))); return false; } catch { return true; } }"
            )
            shared_worker_blocked = await page.evaluate(
                "() => { try { new SharedWorker(URL.createObjectURL(new Blob(['']))); return false; } "
                "catch { return true; } }"
            )
            await page.wait_for_timeout(100)

            assert websocket_blocked is True
            assert webtransport_blocked is True
            assert worker_blocked is True
            assert shared_worker_blocked is True
            assert cdp_decisions == [True, False]
            assert not cdp_tasks
            assert cast(Any, allowed_handler).received_paths == [
                "/",
                "/during",
                "/same-origin-redirect",
                "/passive-after-detach",
            ]
            assert cast(Any, blocked_handler).received_paths == []
            await context.close()
            await browser.close()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
