"""The guarded file download names the file from any RFC 6266 Content-Disposition filename form."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.api import files as files_mod
from skyvern.forge.sdk.core.http_request_authorization import RunScopedRedirectHopAuthorizer

_SCOPE = "run-scope-1"
_BODY = b"%PDF-1.4 synthetic\n"


@pytest.mark.parametrize(
    ("content_disposition", "expected"),
    [
        ('attachment; filename="report.pdf"', "report.pdf"),
        ("attachment; filename=report.pdf", "report.pdf"),
        ("attachment;filename=report.pdf;size=1234", "report.pdf"),
        ('attachment; filename="report.pdf"; size="1234"', "report.pdf"),
        ("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf", "résumé.pdf"),
        ("attachment; filename=\"resume.pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf", "résumé.pdf"),
    ],
)
def test_download_filename_reads_every_content_disposition_form(content_disposition: str, expected: str) -> None:
    headers = {"Content-Disposition": content_disposition, "Content-Type": "application/octet-stream"}

    assert files_mod._determine_download_filename(None, headers, "https://example.com/export?id=42") == expected


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", "attachment; filename=invoice-2024.pdf")
        self.send_header("Content-Length", str(len(_BODY)))
        self.end_headers()
        self.wfile.write(_BODY)


@pytest.fixture
def local_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", [*settings.ALLOWED_HOSTS, "127.0.0.1"])
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_guarded_fetch_keeps_unquoted_content_disposition_filename(local_server: str) -> None:
    url = f"{local_server}/export?id=42"
    response = await files_mod.fetch_file_bytes(
        url,
        authorize_request_hop=RunScopedRedirectHopAuthorizer(download_scope=_SCOPE),
        download_scope=_SCOPE,
        approved_initial_url=url,
    )

    assert response.body == _BODY
    assert response.filename == "invoice-2024.pdf"
