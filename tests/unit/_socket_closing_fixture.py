from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiohttp import web
from playwright.async_api import Page, async_playwright

BODY_SENTINEL = "BODY-SENTINEL-7f3a"
_FIXTURE_PAGE = f"""<html><body>
<img src="/broken.png">
<button id="view" onclick="view()">View</button>
<button id="fast" onclick="fetch('/ok')">Fast</button>
<button id="slow" onclick="fetch('/slow')">Slow</button>
<script>
function view() {{
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/report?token=request-secret&sessionid=sess-secret#frag");
  xhr.send("{BODY_SENTINEL}");
}}
</script>
</body></html>"""


async def _fixture_index(_request: web.Request) -> web.Response:
    return web.Response(text=_FIXTURE_PAGE, content_type="text/html")


async def _fixture_ok(request: web.Request) -> web.Response:
    if request.path == "/slow":
        await asyncio.sleep(20)
    return web.Response(text="ok")


async def _fixture_close_socket(request: web.Request) -> web.Response:
    await request.read()
    assert request.transport is not None
    request.transport.close()
    return web.Response()


@asynccontextmanager
async def socket_closing_fixture_page() -> AsyncIterator[tuple[Page, str]]:
    app = web.Application()
    app.router.add_get("/", _fixture_index)
    app.router.add_get("/ok", _fixture_ok)
    app.router.add_get("/slow", _fixture_ok)
    app.router.add_get("/broken.png", _fixture_close_socket)
    app.router.add_post("/report", _fixture_close_socket)
    # aiohttp keeps a disconnected client's handler running by default, so aborted /slow fetches would hold cleanup.
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                yield page, f"http://127.0.0.1:{runner.addresses[0][1]}"
            finally:
                await browser.close()
    finally:
        await runner.cleanup()
