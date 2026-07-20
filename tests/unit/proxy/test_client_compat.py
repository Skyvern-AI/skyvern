"""Real CDP-client compatibility through the proxy in pass-through mode.

Runs a real CdpProxyServer over a real headless Chrome (the local-Chrome upstream
adapter) and drives it with the clients customers actually point at us: Playwright for
Python, Playwright for JS, puppeteer, and a raw CDP websocket client. Each one really
opens a page and reads it back, so "compatible" means a client's own protocol code was
satisfied end to end — not that a hand-rolled fake of it was.

Hermetic on purpose: the page under test is a data: URL, so a run touches no network
and no third-party site, and the only browser involved is the pinned one the CI job
installs. Nothing here sleeps to synchronize; every wait is a bounded wait-for-condition.

Missing Chrome/node/JS clients skip locally and FAIL in CI (see _e2e_gate) — this suite
is the merge gate for the proxy, and a gate that skips itself is not a gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request
from typing import Any, AsyncIterator

import pytest
import websockets

from skyvern.proxy.adapters.local_chrome import LocalChromeUpstreamBrowser, find_local_chrome_executable
from skyvern.proxy.adapters.memory import AllowAllAuth, ForwardAllEventPolicy, InMemorySessionRegistry, NoOpMetrics
from skyvern.proxy.adapters.websocket_server import CdpProxyServer
from skyvern.proxy.core.session import ResolvedSession
from tests.unit.proxy._e2e_gate import (
    JS_CLIENT_DIR,
    find_node,
    js_clients_installed,
    playwright_python_available,
    require,
    run_js_client,
)

SESSION_ID = "s1"

# The one page every client loads. A data: URL keeps the suite hermetic and keeps any
# real site's name out of an OSS-synced test.
TEST_PAGE = "data:text/html,<title>proxied</title><h1>hello</h1>"

FRAME_TIMEOUT_SECONDS = 30.0

# Resolves on the page's own load event (or immediately, if it already fired), so the
# raw client synchronizes on the browser rather than on the clock.
_TITLE_AFTER_LOAD = """
new Promise((resolve) => {
  if (document.readyState === 'complete') { resolve(document.title); return; }
  addEventListener('load', () => resolve(document.title), {once: true});
})
"""


@pytest.fixture(autouse=True)
def _require_chrome() -> None:
    require(find_local_chrome_executable() is not None, "no local Chrome/Chromium for the upstream adapter")


def _require_js_clients() -> None:
    require(find_node() is not None, "node is not installed")
    require(js_clients_installed(), f"pinned JS clients not installed: run `npm ci` in {JS_CLIENT_DIR}")


@contextlib.asynccontextmanager
async def _running_proxy() -> AsyncIterator[int]:
    sessions = InMemorySessionRegistry()
    sessions.put(
        ResolvedSession(session_id=SESSION_ID, upstream_adapter="local-chrome", upstream_ws_url="ws://ignored")
    )
    server = CdpProxyServer(
        upstream=LocalChromeUpstreamBrowser(),
        sessions=sessions,
        auth=AllowAllAuth(),
        metrics=NoOpMetrics(),
        event_policy=ForwardAllEventPolicy(),
    )
    async with websockets.serve(
        server._handle_client, "127.0.0.1", 0, max_size=None, process_request=server._process_request
    ) as ws_server:
        yield ws_server.sockets[0].getsockname()[1]


def _http_get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - localhost test fixture
        return json.loads(response.read().decode())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("discovery_path", "client"),
    [
        ("/json/version", "generic-cdp"),  # a client asking for the browser version
        ("/json/version/", "playwright"),  # playwright appends /json/version/ (chromium.js)
        ("/json/list", "raw-cdp"),  # a raw client enumerating targets
    ],
)
async def test_discovery_handshake_reaches_upstream(discovery_path: str, client: str) -> None:
    async with _running_proxy() as port:
        discovery = await asyncio.to_thread(_http_get_json, f"http://127.0.0.1:{port}/{SESSION_ID}{discovery_path}")
        entry = discovery if isinstance(discovery, dict) else discovery[0]
        ws_url = entry["webSocketDebuggerUrl"]
        # Proxy-scoped: points back at the proxy, never at the upstream browser.
        assert ws_url == f"ws://127.0.0.1:{port}/{SESSION_ID}"
        async with websockets.connect(ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=FRAME_TIMEOUT_SECONDS))
        assert response["id"] == 1
        assert "product" in response["result"]  # a real upstream Chrome answered


@pytest.mark.asyncio
async def test_real_playwright_python_drives_a_page_through_the_proxy() -> None:
    # require() first: importorskip alone would skip straight past the E2E gate, which
    # is the failure this suite exists to make impossible.
    require(playwright_python_available(), "playwright for python is not installed")
    playwright_api = pytest.importorskip("playwright.async_api")
    async with _running_proxy() as port:
        async with playwright_api.async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}/{SESSION_ID}")
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
                await page.goto(TEST_PAGE)
                assert await page.title() == "proxied"
            finally:
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "client", "scheme"),
    [
        # playwright-js resolves discovery relative to the endpoint, so it takes the
        # http URL; puppeteer must be given the ws endpoint (see the browserURL test).
        ("playwright_client.mjs", "playwright-js", "http"),
        ("puppeteer_client.mjs", "puppeteer", "ws"),
    ],
)
async def test_real_js_client_drives_a_page_through_the_proxy(script: str, client: str, scheme: str) -> None:
    """The pinned JS clients run their own protocol code against the proxy, rather than
    a Python imitation of it — which is the only way a claim of compatibility means
    anything to someone pointing that client at us."""
    _require_js_clients()
    async with _running_proxy() as port:
        result = await asyncio.to_thread(run_js_client, script, f"{scheme}://127.0.0.1:{port}/{SESSION_ID}")

    assert result.returncode == 0, f"{client} failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"client": client, "title": "proxied", "heading": "hello"}


@pytest.mark.asyncio
async def test_puppeteer_browser_url_cannot_address_a_path_scoped_session() -> None:
    """A proven client limitation, pinned so nobody mistakes it for a proxy bug.

    puppeteer resolves discovery as `new URL('/json/version', browserURL)`
    (BrowserConnector.js) — an absolute path, so the session prefix in the URL is
    discarded and it asks the proxy root for a session it cannot name. Callers must use
    browserWSEndpoint, which keeps the path. If a future puppeteer resolves discovery
    relatively, this test fails and browserURL becomes supportable.
    """
    _require_js_clients()
    async with _running_proxy() as port:
        result = await asyncio.to_thread(run_js_client, "puppeteer_client.mjs", f"http://127.0.0.1:{port}/{SESSION_ID}")

    # Asserting the REASON, not just a non-zero exit: any crash would satisfy "it
    # failed", including one that has nothing to do with the discovery path. A future
    # puppeteer that resolves discovery relatively lands here as a clear signal that
    # browserURL has become supportable, rather than as a vague red.
    assert result.returncode != 0, (
        "puppeteer browserURL unexpectedly reached the session — if puppeteer now resolves "
        f"/json/version relatively, browserURL is supportable and this test should go.\nstdout={result.stdout}"
    )
    assert "TargetCloseError" in result.stderr or "Protocol error" in result.stderr, (
        f"puppeteer browserURL failed for an unexpected reason (expected the proxy to close a "
        f"session-less connection):\nstdout={result.stdout}\nstderr={result.stderr}"
    )


async def _command(
    ws: websockets.ClientConnection,
    command_id: int,
    method: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Sends one CDP command and returns its response, skipping the events that arrive
    first. Waits on the matching id rather than on a duration."""
    message: dict[str, Any] = {"id": command_id, "method": method}
    if params is not None:
        message["params"] = params
    if session_id is not None:
        message["sessionId"] = session_id
    await ws.send(json.dumps(message))

    async def until_response() -> dict[str, Any]:
        while True:
            frame: dict[str, Any] = json.loads(await ws.recv())
            if frame.get("id") == command_id:
                return frame

    return await asyncio.wait_for(until_response(), timeout=FRAME_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_raw_cdp_client_drives_a_page_through_the_proxy() -> None:
    """The flat-session path no client library exposes: create a target, attach to it,
    and evaluate in the attached session. Exercises the proxy's own session routing and
    request-id remapping (SKY-12500), which Playwright otherwise hides."""
    async with _running_proxy() as port:
        async with websockets.connect(f"ws://127.0.0.1:{port}/{SESSION_ID}", max_size=None) as ws:
            # Opened blank and navigated explicitly: createTarget answers as soon as the
            # target exists, so a target created straight at the page can still be on
            # about:blank (readyState already 'complete', title already '') when the
            # evaluate below lands. Navigating separately means the document under test
            # is the committed one, with no ordering left to get lucky with.
            created = await _command(ws, 1, "Target.createTarget", {"url": "about:blank"})
            target_id = created["result"]["targetId"]

            attached = await _command(ws, 2, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
            cdp_session = attached["result"]["sessionId"]

            await _command(ws, 3, "Page.enable", session_id=cdp_session)
            await _command(ws, 4, "Page.navigate", {"url": TEST_PAGE}, session_id=cdp_session)

            evaluated = await _command(
                ws,
                5,
                "Runtime.evaluate",
                {"expression": _TITLE_AFTER_LOAD, "awaitPromise": True},
                session_id=cdp_session,
            )

    assert evaluated["result"]["result"]["value"] == "proxied"
