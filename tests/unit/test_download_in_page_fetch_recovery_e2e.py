"""E2E regression for same-origin browser-context recovery of a gated download.

Uses real headless Chromium and a local HTTPS server. The fixture verifies browser-context fetch
behavior, not CDP adoption semantics.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import ssl
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import skyvern.webeye.cdp_download_interceptor as mod
from skyvern.webeye.utils.page import SkyvernFrame


def _deps_available() -> bool:
    try:
        import cryptography  # noqa: F401
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _deps_available(),
    reason="Requires Playwright chromium (playwright install chromium) and cryptography",
)


def _pdf_bytes(name: str) -> bytes:
    """A minimal valid PDF embedding *name*, so a recovered file is distinguishable from the gate page."""
    stream = f"BT /F1 18 Tf 20 100 Td ({name}) Tj ET".encode()
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream endobj\n"
        b"xref\n0 5\n0000000000 65535 f \ntrailer<</Root 1 0 R/Size 5>>\nstartxref\n0\n%%EOF\n"
    )


_RECEIPT_PDF = _pdf_bytes("Receipt")
_GATE_HTML = (
    b"<!DOCTYPE html><html><head><title>Sign in</title></head>"
    b"<body><form method='post' action='/login'>session expired</form></body></html>"
)
_LANDING_HTML = b"<!doctype html><html><body>ready</body></html>"
_REDIRECT_FINAL_HITS = 0
_CHUNKED_SERVED = 0


def _self_signed_cert() -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    tmp = Path(tempfile.mkdtemp())
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


class _Handler(BaseHTTPRequestHandler):
    """Serve PDF bytes to browser-context fetches and an HTML gate to plain clients."""

    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        global _REDIRECT_FINAL_HITS, _CHUNKED_SERVED
        if self.headers.get("x-skyvern-recovery-marker") is not None:
            self.send_error(400, "recovery marker must be stripped")
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/redirect.pdf":
            self.send_response(302)
            self.send_header("Location", "/final.pdf")
            self.end_headers()
            return
        if parsed.path == "/final.pdf":
            _REDIRECT_FINAL_HITS += 1
            body = _RECEIPT_PDF
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/chunked.bin":
            payload = b"x" * (256 * 1024)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            try:
                for _ in range(32):
                    self.wfile.write(payload)
                    self.wfile.flush()
                    _CHUNKED_SERVED += len(payload)
                    threading.Event().wait(0.005)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass
            return
        if parsed.path == "/empty.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path == "/receipt.pdf":
            is_browser = (
                self.headers.get("Sec-Fetch-Site") is not None or self.headers.get("Sec-Fetch-Mode") is not None
            )
            body, ctype = (_RECEIPT_PDF, "application/pdf") if is_browser else (_GATE_HTML, "text/html; charset=utf-8")
        else:
            body, ctype = _LANDING_HTML, "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if parsed.path == "/receipt.pdf":
            self.send_header("Content-Disposition", "attachment; filename=receipt.pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest_asyncio.fixture
async def cdp_https_site():
    """Serve HTTPS and provide a real Chromium context with a page on the origin."""
    from playwright.async_api import async_playwright

    cert_path, key_path = _self_signed_cert()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(cert_path, key_path)
    server.socket = ssl_ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--ignore-certificate-errors", "--no-proxy-server", "--proxy-bypass-list=*"],
            proxy={"server": "direct://"},
        )
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        await page.goto(f"https://127.0.0.1:{port}/", wait_until="domcontentloaded")
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
        redirect_hop_authorizer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_host_client_hits_gate_but_in_page_fetch_gets_pdf(cdp_https_site: Any) -> None:
    """Seam reality: the worker HTTP client lands on the gate; the browser in-page fetch gets the PDF."""
    _context, page, port = cdp_https_site
    url = f"https://127.0.0.1:{port}/receipt.pdf"

    def _host_get() -> bytes:
        insecure = ssl.create_default_context()
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, context=insecure, timeout=10) as resp:
            return resp.read()

    host_body = await asyncio.to_thread(_host_get)
    assert b"<html" in host_body.lower() and not host_body.startswith(b"%PDF-")

    recovered = await SkyvernFrame.read_http_url_bytes(page=page, url=url, max_size_bytes=10 * 1024 * 1024)
    assert recovered is not None and recovered.startswith(b"%PDF-") and b"(Receipt)" in recovered


@pytest.mark.asyncio
async def test_in_page_fetch_redirect_error_fails_closed(cdp_https_site: Any) -> None:
    global _REDIRECT_FINAL_HITS
    _REDIRECT_FINAL_HITS = 0
    _context, page, port = cdp_https_site
    result = await SkyvernFrame.read_http_url_bytes(page, f"https://127.0.0.1:{port}/redirect.pdf", redirect="error")
    assert result is None
    assert _REDIRECT_FINAL_HITS == 0


@pytest.mark.asyncio
async def test_in_page_fetch_chunked_response_stops_at_cap(cdp_https_site: Any) -> None:
    global _CHUNKED_SERVED
    _CHUNKED_SERVED = 0
    _context, page, port = cdp_https_site
    result = await SkyvernFrame.read_http_url_bytes(page, f"https://127.0.0.1:{port}/chunked.bin", max_size_bytes=1024)
    assert result is None
    assert _CHUNKED_SERVED < 32 * 256 * 1024


@pytest.mark.asyncio
async def test_in_page_fetch_empty_response_is_empty_bytes(cdp_https_site: Any) -> None:
    _context, page, port = cdp_https_site
    assert await SkyvernFrame.read_http_url_bytes(page, f"https://127.0.0.1:{port}/empty.bin") == b""


@pytest.mark.asyncio
async def test_download_url_directly_recovers_gated_download_in_page(cdp_https_site: Any, tmp_path: Path) -> None:
    """Full handler: host fetch returns the HTML masquerade; recovery lands the real PDF in-page."""
    context, page, port = cdp_https_site
    url = f"https://127.0.0.1:{port}/receipt.pdf"

    interceptor = _make_interceptor(str(tmp_path))
    interceptor._browser_context = context
    await interceptor.enable_for_page(page)
    guarded_fetch = AsyncMock(return_value=MagicMock(body=_GATE_HTML, content_type="text/html", filename="receipt.pdf"))
    try:
        with patch.object(mod.file_api, "fetch_file_bytes", guarded_fetch, create=True):
            await interceptor._download_url_directly(url, "receipt.pdf")
    finally:
        await interceptor.disable()

    saved = list(tmp_path.iterdir())
    assert len(saved) == 1, "the gated download was not recovered via the in-page same-origin fetch"
    body = saved[0].read_bytes()
    assert body.startswith(b"%PDF-") and b"(Receipt)" in body


@pytest.mark.asyncio
async def test_no_same_origin_page_stays_fail_closed(cdp_https_site: Any, tmp_path: Path) -> None:
    """Fail-closed: with no same-origin frame, recovery is unavailable and the masquerade failure stands."""
    context, page, port = cdp_https_site
    url = f"https://127.0.0.1:{port}/receipt.pdf"
    await page.goto("about:blank", wait_until="domcontentloaded")

    interceptor = _make_interceptor(str(tmp_path))
    interceptor._browser_context = context
    guarded_fetch = AsyncMock(return_value=MagicMock(body=_GATE_HTML, content_type="text/html", filename="receipt.pdf"))
    with patch.object(mod.file_api, "fetch_file_bytes", guarded_fetch, create=True):
        await interceptor._download_url_directly(url, "receipt.pdf")

    assert list(tmp_path.iterdir()) == []
