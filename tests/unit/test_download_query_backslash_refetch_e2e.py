"""Real-Chromium E2E: the CDP download re-fetch saves a download whose URL carries a Windows-style
path (backslashes) in a query parameter.

Exercises the actual interceptor -> guarded re-fetch (validate+pin SSRF guard, encode_url wire form,
aiohttp transport) -> run-directory persistence chain, against a local HTTP server and a real headless
Chromium context on the origin. Fixtures are synthetic — no customer identifiers, WPIDs, run IDs, or
customer URLs. Mirrors the harness of ``test_download_in_page_fetch_recovery_e2e`` but drives the real
guarded fetch rather than a patched one, so the query-backslash acceptance is proven end to end.
"""

from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

import skyvern.webeye.cdp_download_interceptor as mod
from skyvern.config import settings
from skyvern.forge.sdk.core.http_request_authorization import RunScopedRedirectHopAuthorizer

# A Windows/UNC-style path in a query parameter, the shape ASP.NET download endpoints emit. Synthetic.
_WINDOWS_QUERY = r"\V\segment\0\sample_metered.zip.gpg"
_SAMPLE_BODY = b"SKYVERN-SYNTHETIC-DOWNLOAD-BYTES\n"
_RECEIVED_QUERY: list[str] = []


def _deps_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _deps_available(), reason="Requires Playwright chromium (playwright install chromium)"
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            body = b"<!doctype html><html><body>ready</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        _RECEIVED_QUERY.append(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="sample.bin"')
        self.send_header("Content-Length", str(len(_SAMPLE_BODY)))
        self.end_headers()
        self.wfile.write(_SAMPLE_BODY)


@pytest_asyncio.fixture
async def http_site(monkeypatch: pytest.MonkeyPatch) -> Any:
    _RECEIVED_QUERY.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", [*settings.ALLOWED_HOSTS, "127.0.0.1"])

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-proxy-server", "--proxy-bypass-list=*"],
            proxy={"server": "direct://"},
        )
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        try:
            yield context, page, port
        finally:
            await browser.close()
            server.shutdown()


def _make_interceptor(output_dir: str) -> mod.CDPDownloadInterceptor:
    monitor = MagicMock()
    monitor.authorize_request.return_value = True
    return mod.CDPDownloadInterceptor(
        output_dir=output_dir,
        network_egress_monitor=monitor,
        redirect_hop_authorizer=RunScopedRedirectHopAuthorizer(download_scope="e2e-scope"),
    )


@pytest.mark.asyncio
async def test_cdp_refetch_saves_query_backslash_download(http_site: Any, tmp_path: Path) -> None:
    context, page, port = http_site
    url = f"http://127.0.0.1:{port}/ELABSTelFileDownload.aspx?TelFile={_WINDOWS_QUERY}"

    interceptor = _make_interceptor(str(tmp_path))
    interceptor._browser_context = context
    await interceptor.enable_for_page(page)
    try:
        await interceptor._download_url_directly(url, "sample.bin")
    finally:
        await interceptor.disable()

    saved = list(tmp_path.iterdir())
    assert len(saved) == 1, "the query-backslash download was not saved through the guarded re-fetch"
    assert saved[0].read_bytes() == _SAMPLE_BODY
    # The server received the identical Windows path the browser reported (percent-encoded on the wire).
    assert _RECEIVED_QUERY, "the guarded re-fetch never reached the server"
    assert urllib.parse.parse_qs(_RECEIVED_QUERY[-1])["TelFile"] == [_WINDOWS_QUERY]
