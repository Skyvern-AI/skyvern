"""The guarded download re-fetch accepts benign Windows-path backslashes in the query/fragment.

A ``file_download`` whose href carries a Windows-style path in a query parameter (backslashes) was
refused before any network request: the origin precondition in the guarded re-fetch rejects any URL
containing a backslash, and with the browser's own download denied there is no fallback, so the file
is never saved.

These tests drive the REAL production authorizer (``RunScopedRedirectHopAuthorizer``) — NOT a
permissive stub — because that authorizer's first-hop binding independently canonicalizes the target
(``canonicalize_effect_target`` → ``canonicalize_origin``) and would otherwise reject the same
backslash URL after the precheck. They exercise the real chain (validate+pin SSRF guard, the run-scoped
authorization/binding, ``encode_url`` wire serialization, aiohttp transport) against a local server.
Fixtures are synthetic — no customer identifiers, WPIDs, run IDs, or customer URLs.
"""

from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.api import files as files_mod
from skyvern.forge.sdk.browser_action_policy import canonicalize_origin
from skyvern.forge.sdk.core.http_request_authorization import RunScopedRedirectHopAuthorizer

HttpException = files_mod.HttpException
fetch_file_bytes = files_mod.fetch_file_bytes
_origin_authorizable_url = files_mod._origin_authorizable_url

# A Windows/UNC-style path in a query parameter, the shape ASP.NET download endpoints emit. Synthetic.
_WINDOWS_QUERY = r"\V\segment\0\sample_metered.zip.gpg"
_ENCODED_QUERY = "%5CV%5Csegment%5C0%5Csample_metered.zip.gpg"
_SAMPLE_BODY = b"SKYVERN-SYNTHETIC-DOWNLOAD-BYTES\n"
_SCOPE = "run-scope-1"


class _RecordingHandler(BaseHTTPRequestHandler):
    """Serve a small non-empty body; record the raw query; 302 to a denied origin on /redirect-denied."""

    received_raw_query: list[str] = []

    def log_message(self, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/redirect-denied":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/blocked")
            self.end_headers()
            return
        type(self).received_raw_query.append(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="sample.bin"')
        self.send_header("Content-Length", str(len(_SAMPLE_BODY)))
        self.end_headers()
        self.wfile.write(_SAMPLE_BODY)


@pytest.fixture
def local_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    _RecordingHandler.received_raw_query = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # The SSRF guard blocks loopback by default; the local fixture is explicitly allowed, exactly as
    # the existing adopted-session download integration test does.
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", [*settings.ALLOWED_HOSTS, "127.0.0.1"])
    try:
        yield f"http://127.0.0.1:{port}", _RecordingHandler
    finally:
        server.shutdown()


def _authz() -> RunScopedRedirectHopAuthorizer:
    return RunScopedRedirectHopAuthorizer(download_scope=_SCOPE)


# --- pure helper: what the origin check / authorizer is allowed to see -------------------------


def test_origin_authorizable_url_encodes_only_query_and_fragment() -> None:
    raw = "https://host.example/ELABSTelFileDownload.aspx?TelFile=" + _WINDOWS_QUERY
    normalized = _origin_authorizable_url(raw)
    assert canonicalize_origin(raw) is None
    assert "\\" not in normalized
    assert canonicalize_origin(normalized) is not None
    assert canonicalize_origin(_origin_authorizable_url("https://host.example/p#a\\b")) is not None


@pytest.mark.parametrize(
    "raw",
    [
        "https://good.example\\@evil.example/f?x=1",
        "https:/\\good.example/f?x=1",
        "https://good.example\\.evil.example/?x=1",
        "https://host.example/a\\b?x=1",
        "https://host.example/a\\b",
    ],
)
def test_origin_authorizable_url_keeps_scheme_authority_path_fail_closed(raw: str) -> None:
    assert canonicalize_origin(_origin_authorizable_url(raw)) is None


def test_origin_authorizable_url_leaves_ordinary_urls_untouched() -> None:
    for url in ("https://host.example/path", "https://host.example/p?x=1&y=2", "https://host.example/"):
        assert _origin_authorizable_url(url) == url


# --- real production authorizer + real re-fetch chain ------------------------------------------


@pytest.mark.asyncio
async def test_production_authorizer_saves_benign_query_backslash(local_server: Any) -> None:
    base, handler = local_server
    url = f"{base}/ELABSTelFileDownload.aspx?TelFile={_WINDOWS_QUERY}"
    response = await fetch_file_bytes(
        url,
        authorize_request_hop=_authz(),
        download_scope=_SCOPE,
        approved_initial_url=url,
        normalize_query_backslashes=True,
    )
    assert response.body == _SAMPLE_BODY and response.filename == "sample.bin"
    # The run-scoped authorizer admitted the hop AND the server received the exact Windows path.
    assert handler.received_raw_query, "the guarded re-fetch never reached the server"
    assert urllib.parse.parse_qs(handler.received_raw_query[-1])["TelFile"] == [_WINDOWS_QUERY]


@pytest.mark.asyncio
async def test_production_authorizer_saves_already_encoded_query(local_server: Any) -> None:
    base, handler = local_server
    url = f"{base}/dl.aspx?TelFile={_ENCODED_QUERY}"
    response = await fetch_file_bytes(
        url,
        authorize_request_hop=_authz(),
        download_scope=_SCOPE,
        approved_initial_url=url,
        normalize_query_backslashes=True,
    )
    assert response.body == _SAMPLE_BODY
    # Already-encoded %5C is not double-encoded; the server decodes it back to the backslash path.
    assert urllib.parse.parse_qs(handler.received_raw_query[-1])["TelFile"] == [_WINDOWS_QUERY]


@pytest.mark.asyncio
async def test_default_off_rejected_at_precheck(local_server: Any) -> None:
    base, handler = local_server
    url = f"{base}/dl.aspx?TelFile={_WINDOWS_QUERY}"
    # Default (opt-in off): the precheck refuses before any network request or authorizer call.
    with pytest.raises(HttpException, match="browser-canonicalizable"):
        await fetch_file_bytes(url, authorize_request_hop=_authz(), download_scope=_SCOPE, approved_initial_url=url)
    assert handler.received_raw_query == []


@pytest.mark.asyncio
async def test_wrong_first_target_binding_rejected(local_server: Any) -> None:
    base, handler = local_server
    port = int(urllib.parse.urlsplit(base).port)
    url = f"{base}/dl.aspx?TelFile={_WINDOWS_QUERY}"
    # approved_initial_url has a DIFFERENT origin (port); the run-scoped first-hop binding must refuse,
    # proving the opt-in did not weaken the target/initial origin binding.
    other_initial = f"http://127.0.0.1:{port + 1}/dl.aspx?TelFile={_WINDOWS_QUERY}"
    with pytest.raises(PermissionError):
        await fetch_file_bytes(
            url,
            authorize_request_hop=_authz(),
            download_scope=_SCOPE,
            approved_initial_url=other_initial,
            normalize_query_backslashes=True,
        )
    assert handler.received_raw_query == []


@pytest.mark.asyncio
async def test_disallowed_redirect_fails_closed(local_server: Any) -> None:
    base, _ = local_server
    url = f"{base}/redirect-denied?TelFile={_WINDOWS_QUERY}"
    # First hop is admitted (same origin) and reaches the server, which 302s to a link-local metadata
    # address; the redirect hop is still validated and pinned, so the denied origin fails closed.
    with pytest.raises(Exception) as excinfo:
        await fetch_file_bytes(
            url,
            authorize_request_hop=_authz(),
            download_scope=_SCOPE,
            approved_initial_url=url,
            normalize_query_backslashes=True,
        )
    assert type(excinfo.value).__name__ in {"BlockedHost", "SkyvernHTTPException", "HttpException"}


@pytest.mark.asyncio
async def test_ssrf_target_refused_with_opt_in() -> None:
    # A link-local metadata address is refused by the host guard before the authorizer, even opted in.
    with pytest.raises(Exception) as excinfo:
        await fetch_file_bytes(
            "http://169.254.169.254/latest/meta-data?p=\\x\\y",
            authorize_request_hop=_authz(),
            download_scope=_SCOPE,
            approved_initial_url="http://169.254.169.254/latest/meta-data?p=\\x\\y",
            normalize_query_backslashes=True,
        )
    assert type(excinfo.value).__name__ in {"BlockedHost", "SkyvernHTTPException", "HttpException"}


@pytest.mark.asyncio
async def test_ordinary_no_backslash_default_saves(local_server: Any) -> None:
    base, _ = local_server
    url = f"{base}/dl.aspx?x=1"
    response = await fetch_file_bytes(
        url, authorize_request_hop=_authz(), download_scope=_SCOPE, approved_initial_url=url
    )
    assert response.body == _SAMPLE_BODY
