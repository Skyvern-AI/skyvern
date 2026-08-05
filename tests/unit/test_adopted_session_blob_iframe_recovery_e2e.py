"""E2E regression for adopted-session blob-download recovery via the live PDF iframe.

Reproduces the production failure mode where a persistent/adopted (CDP-connected) browser session
fires a Playwright ``download`` event for a client-side ``blob:`` statement, but the download's own
bytes cannot be captured (``save_as`` yields an empty file and the download URL cannot be fetched from
any page/frame). The statement itself is still on screen in a live same-origin ``blob:`` PDF iframe.

Merged code returns no file for this case; the recovery reads the live iframe's blob and lands a valid
PDF. Driven over ``connect_over_cdp`` against a real headless Chromium so the read path is exercised for
real; only the ``Download`` object is a stand-in, because a remote-CDP session's empty ``save_as`` /
unfetchable download URL cannot be reproduced by a single local browser.

Skipped in CI when Playwright browsers are not installed.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from skyvern.webeye.actions.handler import _save_adopted_session_download
from skyvern.webeye.utils.page import (
    SkyvernFrame,
    install_blob_url_retention,
    probe_blob_action_freshness,
    teardown_blob_url_retention,
)


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


def _pdf_for(name: str) -> bytes:
    """A minimal valid PDF embedding the doc name, so distinct statements have distinct bytes."""
    stream = f"BT /F1 18 Tf 20 100 Td ({name}) Tj ET".encode()
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream endobj\n"
        b"xref\n0 5\n0000000000 65535 f \ntrailer<</Root 1 0 R/Size 5>>\nstartxref\n0\n%%EOF\n"
    )


# Renders one same-origin blob: PDF iframe per doc name (title == doc name), like the production DOM.
# ``__renderStatements`` is exposed so a test can mint the blobs AFTER installing retention (so their
# URLs land in the action window's retained Map); ``?docs=`` still auto-renders at load for the
# fail-closed cases that never reach the freshness gate. Returns the minted (fragment-less) blob URLs.
_PAGE = """<!doctype html><html><body style="margin:0"><div id="v"></div>
<script>
window.__renderStatements = async (docs) => {
  const urls = [];
  for (const name of docs.split(',')) {
    const resp = await fetch('/statement.pdf?name=' + encodeURIComponent(name));
    const buf = await resp.arrayBuffer();
    const url = URL.createObjectURL(new Blob([buf], {type: 'application/pdf'}));
    urls.push(url);
    const f = document.createElement('iframe');
    f.width = '100%'; f.height = '300'; f.title = name; f.src = url + '#view=FitH';
    document.getElementById('v').appendChild(f);
  }
  return urls;
};
(async () => {
  const q = new URLSearchParams(location.search).get('docs');
  if (q) await window.__renderStatements(q);
  window.__ready = true;
})();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:  # keep test output clean
        pass

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/statement.pdf":
            name = urllib.parse.parse_qs(parsed.query).get("name", ["doc"])[0]
            body, ctype = _pdf_for(name), "application/pdf"
        else:
            body, ctype = _PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest_asyncio.fixture
async def cdp_adopted():
    """Serve the statement page, launch Chromium with remote debugging, and adopt it over CDP."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with socket.socket() as _s:
        _s.bind(("127.0.0.1", 0))
        cdp_port = _s.getsockname()[1]

    async with async_playwright() as p:
        launcher = await p.chromium.launch(
            headless=True,
            args=[f"--remote-debugging-port={cdp_port}", "--no-proxy-server", "--proxy-bypass-list=*"],
            proxy={"server": "direct://"},
        )
        adopted = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = adopted.contexts[0] if adopted.contexts else await adopted.new_context(accept_downloads=True)

        async def open_page(docs: str | None = None):
            page = await context.new_page()
            suffix = f"/?docs={docs}" if docs else "/"
            await page.goto(f"http://127.0.0.1:{port}{suffix}", wait_until="domcontentloaded")
            for _ in range(50):
                if await page.evaluate("() => window.__ready === true"):
                    break
                await asyncio.sleep(0.1)
            return page, port

        try:
            yield open_page
        finally:
            await adopted.close()
            await launcher.close()
            server.shutdown()


def _unreadable_blob_download(page: Any, port: int, suggested_filename: str) -> MagicMock:
    """A Download whose own bytes are unrecoverable: empty save_as and an unfetchable blob URL.

    Same origin as the live statement iframe(s), a distinct (nonexistent) blob id — mirroring the
    production observation that the download event's blob differs from the live viewer blob.
    """
    download = MagicMock()
    download.url = f"blob:http://127.0.0.1:{port}/00000000-0000-4000-8000-000000000000"
    download.suggested_filename = suggested_filename
    download.page = page
    download.failure = AsyncMock(return_value=None)
    download.save_as = AsyncMock()  # no-op: leaves an empty target, as adopted blob save_as does
    return download


@pytest.mark.asyncio
async def test_recovers_named_statement_from_live_blob_iframe(cdp_adopted, tmp_path) -> None:
    # The download's suggested filename matches the on-screen iframe's title. The blob is minted
    # through the action-window retention wrapper, so it is action-fresh and recovered.
    page, port = await cdp_adopted()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    try:
        await page.evaluate("(d) => window.__renderStatements(d)", "AnnualStatement.pdf")
        download = _unreadable_blob_download(page, port, suggested_filename="AnnualStatement.pdf")
        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-test")
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    assert saved is not None, "named statement was not recovered from the live PDF iframe"
    data = Path(saved).read_bytes()
    assert data.startswith(b"%PDF-") and b"(AnnualStatement.pdf)" in data


@pytest.mark.asyncio
async def test_filename_match_selects_correct_iframe_not_first(cdp_adopted, tmp_path) -> None:
    # Two blob PDF iframes; the DECOY is first in DOM order. Selection must follow the filename match.
    page, port = await cdp_adopted()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    try:
        await page.evaluate("(d) => window.__renderStatements(d)", "DecoyDoc.pdf,AnnualStatement2026.pdf")
        download = _unreadable_blob_download(page, port, suggested_filename="AnnualStatement2026.pdf")
        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-test")
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    assert saved is not None
    data = Path(saved).read_bytes()
    assert b"(AnnualStatement2026.pdf)" in data, "recovered the wrong (decoy) iframe"
    assert b"(DecoyDoc.pdf)" not in data


@pytest.mark.asyncio
async def test_single_iframe_name_mismatch_fails_closed(cdp_adopted, tmp_path) -> None:
    # Exactly one blob PDF iframe on screen, but the suggested filename does not match its title:
    # never assume the visible viewer is the requested statement.
    page, port = await cdp_adopted("AnnualStatement.pdf")
    download = _unreadable_blob_download(page, port, suggested_filename="something-else.pdf")

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-test")

    assert saved is None, "a single name-mismatched blob PDF iframe must fail closed"
    assert list(Path(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_ambiguous_multiple_pdf_iframes_fail_closed(cdp_adopted, tmp_path) -> None:
    # Two PDF blob iframes and a suggested filename that matches neither: never guess.
    page, port = await cdp_adopted("DocOne.pdf,DocTwo.pdf")
    download = _unreadable_blob_download(page, port, suggested_filename="something-else.pdf")

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-test")

    assert saved is None, "ambiguous multiple blob PDF iframes must fail closed"
    assert list(Path(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_duplicate_title_match_fails_closed(cdp_adopted, tmp_path) -> None:
    # Two iframes carry the SAME title that matches the suggested filename: ambiguous, fail closed.
    page, port = await cdp_adopted("Statement.pdf,Statement.pdf")
    download = _unreadable_blob_download(page, port, suggested_filename="Statement.pdf")

    saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-test")

    assert saved is None, "duplicate matching titles must fail closed"
    assert list(Path(tmp_path).iterdir()) == []


# ---------------------------------------------------------------------------
# Action-freshness gate: a named blob PDF iframe is recovered only when its blob was minted through
# the pre-click retention wrapper during this action window (a live key in __skyvernBlobRetention.
# retained). A same-named iframe whose blob was minted outside the window is rejected — a wrong
# document is worse than a visible miss.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_iframe_not_minted_through_retention_fails_closed(cdp_adopted, tmp_path) -> None:
    # The statement blob was minted at page load, before retention install: its URL is not a key in
    # the action window's retained Map, so recovery fails closed even though the title matches.
    page, port = await cdp_adopted("AnnualStatement.pdf")
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    try:
        download = _unreadable_blob_download(page, port, suggested_filename="AnnualStatement.pdf")
        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-test")
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    assert saved is None, "a stale iframe blob not minted through the action window must fail closed"
    assert list(Path(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_inactive_owned_state_probes_as_not_fresh(cdp_adopted, tmp_path) -> None:
    # Stale cleanup can flip an owned state's active flag off (e.g. it neutralizes the closures and then
    # returns on an exception) while retained keys remain in the Map. The freshness probe must require a
    # valid owned AND active state, so those inactive retained keys are never trusted as action-fresh.
    page, _ = await cdp_adopted()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    try:
        urls = await page.evaluate("(d) => window.__renderStatements(d)", "AnnualStatement.pdf")
        blob_url = urls[0]
        # While active, the retained blob is action-fresh (guards the setup is valid).
        active = await probe_blob_action_freshness(page, blob_url, workflow_run_id="wr-test")
        assert active.state_observed is True and active.retained is True
        # Neutralize the closures but leave the retained keys, as an interrupted stale cleanup would.
        await page.evaluate("() => { window.__skyvernBlobRetention.active = false; }")
        inactive = await probe_blob_action_freshness(page, blob_url, workflow_run_id="wr-test")
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    assert inactive.state_observed is False, "an inactive owned state must not be observed as fresh"
    assert inactive.retained is False


@pytest.mark.asyncio
async def test_creator_realm_blob_recovered_when_display_frame_differs(cdp_adopted, tmp_path) -> None:
    # The main realm mints the blob and assigns it to the iframe src; Chromium's PDF viewer renders it
    # in a separate frame with no retention state. Recovery must succeed by probing the creator (main)
    # realm, not only the display frame.
    page, port = await cdp_adopted()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    try:
        await page.evaluate("(d) => window.__renderStatements(d)", "AnnualStatement.pdf")
        download = _unreadable_blob_download(page, port, suggested_filename="AnnualStatement.pdf")
        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-test")
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    assert saved is not None, "creator-realm retention state was not found"
    assert b"(AnnualStatement.pdf)" in Path(saved).read_bytes()


@pytest.mark.asyncio
async def test_retained_iframe_blob_synchronously_revoked_still_recovered(cdp_adopted, tmp_path) -> None:
    # The site revokes the object URL synchronously after minting it; the retention wrapper defers the
    # revoke, so the URL stays a live key in the retained Map and recovery still succeeds this window.
    page, port = await cdp_adopted()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    try:
        urls = await page.evaluate("(d) => window.__renderStatements(d)", "AnnualStatement.pdf")
        await page.evaluate("(u) => URL.revokeObjectURL(u)", urls[0])
        download = _unreadable_blob_download(page, port, suggested_filename="AnnualStatement.pdf")
        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-test")
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    assert saved is not None, "a retained-then-revoked blob must remain recoverable while the window is live"
    assert b"(AnnualStatement.pdf)" in Path(saved).read_bytes()


@pytest.mark.asyncio
async def test_sequential_window_stale_iframe_fails_closed(cdp_adopted, tmp_path) -> None:
    # A prior action window minted and displayed the statement, then tore retention down. In a new
    # window (fresh, empty retained Map) the leftover iframe's blob is not action-fresh: fail closed.
    page, port = await cdp_adopted()
    await install_blob_url_retention(page, workflow_run_id="wr-1")
    await page.evaluate("(d) => window.__renderStatements(d)", "Statement.pdf")
    await teardown_blob_url_retention(page, workflow_run_id="wr-1")

    await install_blob_url_retention(page, workflow_run_id="wr-2")
    try:
        download = _unreadable_blob_download(page, port, suggested_filename="Statement.pdf")
        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-2")
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-2")

    assert saved is None, "a same-named iframe from a prior action window must not be accepted"
    assert list(Path(tmp_path).iterdir()) == []


# ---------------------------------------------------------------------------
# Canonical transient-blob shape: two untitled iframes, no persistent blob PDF
# iframe, and a "View Document" control that mints a PDF Blob transiently, fires
# a download, then revokes the object URL synchronously — so by the time any
# reader runs the blob: URL is already dead. Pre-action blob retention keeps the
# URL alive for the action window so the existing in-page read recovers the bytes.
# ---------------------------------------------------------------------------

_CANON_PAGE = """<!doctype html><html><body style="margin:0">
<iframe title="" src="/frame_a.html" width="100%" height="60"></iframe>
<iframe title="" src="/frame_b.html" width="100%" height="60"></iframe>
<button id="view">View Document</button>
<script>
window.__mintStatement = async (name, type) => {
  const resp = await fetch('/statement.pdf?name=' + encodeURIComponent(name));
  const buf = await resp.arrayBuffer();
  const blob = new Blob([buf], {type: type || 'application/pdf'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);  // synchronous teardown, as the production site does
  return url;
};
window.__ready = true;
</script></body></html>"""


class _CanonHandler(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/statement.pdf":
            name = urllib.parse.parse_qs(parsed.query).get("name", ["doc"])[0]
            body, ctype = _pdf_for(name), "application/pdf"
        elif parsed.path in ("/frame_a.html", "/frame_b.html"):
            body, ctype = b"<!doctype html><html><body>untitled</body></html>", "text/html; charset=utf-8"
        else:
            body, ctype = _CANON_PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest_asyncio.fixture
async def cdp_canonical():
    """Serve the canonical statement page and adopt a real Chromium over CDP."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CanonHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with socket.socket() as _s:
        _s.bind(("127.0.0.1", 0))
        cdp_port = _s.getsockname()[1]

    async with async_playwright() as p:
        launcher = await p.chromium.launch(
            headless=True,
            args=[f"--remote-debugging-port={cdp_port}", "--no-proxy-server", "--proxy-bypass-list=*"],
            proxy={"server": "direct://"},
        )
        adopted = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = adopted.contexts[0] if adopted.contexts else await adopted.new_context(accept_downloads=True)

        async def open_page():
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            for _ in range(50):
                if await page.evaluate("() => window.__ready === true"):
                    break
                await asyncio.sleep(0.1)
            return page, port

        try:
            yield open_page
        finally:
            await adopted.close()
            await launcher.close()
            server.shutdown()


def _savefails_blob_download(page: Any, blob_url: str, suggested_filename: str) -> MagicMock:
    """A Download carrying the real minted blob URL, whose eager save_as yields an empty file.

    The empty save_as models the exact production observation for a remote-CDP adopted session
    ("Adopted-session eager save_as produced an empty file"); everything else — the blob URL and
    the in-page read that recovers it — runs for real.
    """
    download = MagicMock()
    download.url = blob_url
    download.suggested_filename = suggested_filename
    download.page = page
    download.failure = AsyncMock(return_value=None)
    download.save_as = AsyncMock()  # no-op: leaves an empty target, as adopted blob save_as does
    return download


@pytest.mark.asyncio
async def test_canonical_transient_revoked_blob_is_recovered_while_retained(cdp_canonical, tmp_path) -> None:
    page, _ = await cdp_canonical()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    try:
        blob_url = await page.evaluate("(n) => window.__mintStatement(n)", "AnnualStatement.pdf")
        assert isinstance(blob_url, str) and blob_url.startswith("blob:")
        download = _savefails_blob_download(page, blob_url, suggested_filename="AnnualStatement.pdf")
        saved = await _save_adopted_session_download(download, page, tmp_path, workflow_run_id="wr-test")
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    assert saved is not None, "canonical transient blob was not recovered while retained"
    data = Path(saved).read_bytes()
    assert data.startswith(b"%PDF-") and b"(AnnualStatement.pdf)" in data


@pytest.mark.asyncio
async def test_pdf_blob_stays_readable_while_retained_then_dies_on_teardown(cdp_canonical, tmp_path) -> None:
    page, _ = await cdp_canonical()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    blob_url = await page.evaluate("(n) => window.__mintStatement(n)", "Statement.pdf")

    retained = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url=blob_url, probe=True)
    assert retained is not None and retained.startswith(b"%PDF-"), "retained PDF blob should be readable"

    await teardown_blob_url_retention(page, workflow_run_id="wr-test")
    after = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url=blob_url, probe=True)
    assert after is None, "teardown must perform the deferred revoke so the URL is no longer readable"


@pytest.mark.asyncio
async def test_native_revoke_restored_after_teardown(cdp_canonical, tmp_path) -> None:
    page, _ = await cdp_canonical()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    # With the wrappers restored, a fresh mint+revoke must behave natively: the URL dies immediately.
    blob_url = await page.evaluate("(n) => window.__mintStatement(n)", "Statement.pdf")
    after = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url=blob_url, probe=True)
    assert after is None, "after teardown the native synchronous revoke must take effect again"


@pytest.mark.asyncio
async def test_native_method_objects_restored_by_identity_after_teardown(cdp_canonical, tmp_path) -> None:
    # Teardown must restore the exact native function objects, not a bound wrapper: identity, name and
    # toString must be indistinguishable from the pre-install originals for the persistent session.
    page, _ = await cdp_canonical()
    await page.evaluate(
        "() => { window.__origCreate = URL.createObjectURL; window.__origRevoke = URL.revokeObjectURL; }"
    )
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    identity = await page.evaluate(
        """() => ({
            createSame: URL.createObjectURL === window.__origCreate,
            revokeSame: URL.revokeObjectURL === window.__origRevoke,
        })"""
    )
    assert identity["createSame"], "createObjectURL must be restored to the exact native function object"
    assert identity["revokeSame"], "revokeObjectURL must be restored to the exact native function object"


@pytest.mark.asyncio
async def test_non_pdf_blob_is_not_retained(cdp_canonical, tmp_path) -> None:
    page, _ = await cdp_canonical()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    try:
        # A non-PDF blob (image) is an unrelated download: it must revoke natively, not be retained.
        blob_url = await page.evaluate("(n) => window.__mintStatement(n, 'image/png')", "logo.png")
        after = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url=blob_url, probe=True)
        assert after is None, "an unrelated non-PDF blob must not be retained"
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-test")


@pytest.mark.asyncio
async def test_retention_preserves_native_return_values(cdp_canonical, tmp_path) -> None:
    page, _ = await cdp_canonical()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    try:
        shapes = await page.evaluate(
            """() => {
              const b = new Blob([new Uint8Array([37,80,68,70])], {type: 'application/pdf'});
              const url = URL.createObjectURL(b);
              const revokeReturn = URL.revokeObjectURL(url);
              return { urlOk: typeof url === 'string' && url.startsWith('blob:'),
                       revokeUndefined: revokeReturn === undefined };
            }"""
        )
        assert shapes["urlOk"], "createObjectURL must still return a real blob: URL string"
        assert shapes["revokeUndefined"], "revokeObjectURL must still return undefined"
    finally:
        await teardown_blob_url_retention(page, workflow_run_id="wr-test")


@pytest.mark.asyncio
async def test_teardown_is_idempotent(cdp_canonical, tmp_path) -> None:
    page, _ = await cdp_canonical()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    await teardown_blob_url_retention(page, workflow_run_id="wr-test")
    # A second teardown with no retention installed must be a harmless no-op.
    await teardown_blob_url_retention(page, workflow_run_id="wr-test")


# ---------------------------------------------------------------------------
# State ownership: install/probe/teardown only act on a validly branded, complete Skyvern-owned state.
# A foreign page global sharing the name is never mutated and never trusted; a valid stale owned state
# (a prior window that never tore down) is settled and cleared on rearm so no key leaks across windows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_same_name_global_is_untouched_and_not_trusted(cdp_canonical) -> None:
    # A page owns a truthy window.__skyvernBlobRetention with a lying retained.has. Install/teardown must
    # not replace it or clobber the URL methods, and the freshness probe must fail closed on it.
    page, port = await cdp_canonical()
    await page.evaluate(
        """() => {
            window.__foreignSentinel = { retained: { has: () => true }, note: 'page-owned' };
            window.__skyvernBlobRetention = window.__foreignSentinel;
            window.__origCreate = URL.createObjectURL;
            window.__origRevoke = URL.revokeObjectURL;
        }"""
    )

    await install_blob_url_retention(page, workflow_run_id="wr-test")
    await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    state = await page.evaluate(
        """() => ({
            sameProp: window.__skyvernBlobRetention === window.__foreignSentinel,
            stillPresent: !!window.__skyvernBlobRetention && window.__skyvernBlobRetention.note === 'page-owned',
            createUntouched: URL.createObjectURL === window.__origCreate,
            revokeUntouched: URL.revokeObjectURL === window.__origRevoke,
        })"""
    )
    assert state["sameProp"], "install/teardown must not replace a foreign same-name global"
    assert state["stillPresent"], "the foreign property must not be deleted or mutated"
    assert state["createUntouched"] and state["revokeUntouched"], "URL methods must not be clobbered"

    fresh = await probe_blob_action_freshness(page, f"blob:http://127.0.0.1:{port}/x", workflow_run_id="wr-test")
    assert fresh.state_observed is False and fresh.retained is False, "a foreign global must not read as owned/fresh"


@pytest.mark.asyncio
async def test_rearm_over_stale_owned_state_clears_prior_window(cdp_canonical) -> None:
    # A prior window installed retention and minted a statement, then never tore down (crash/cancel).
    # Re-installing must settle+restore+clear the stale state and rearm with an empty retained Map, so
    # the prior window's blob key is not action-fresh and the exact natives survive by identity.
    page, _ = await cdp_canonical()
    await page.evaluate(
        "() => { window.__origCreate = URL.createObjectURL; window.__origRevoke = URL.revokeObjectURL; }"
    )

    await install_blob_url_retention(page, workflow_run_id="wr-1")
    stale_url = await page.evaluate("(n) => window.__mintStatement(n)", "Statement.pdf")
    assert isinstance(stale_url, str) and stale_url.startswith("blob:")
    fresh_before = await probe_blob_action_freshness(page, stale_url, workflow_run_id="wr-1")
    assert fresh_before.state_observed and fresh_before.retained, "the minted blob should be fresh within its window"

    # Rearm WITHOUT tearing down the prior window.
    await install_blob_url_retention(page, workflow_run_id="wr-2")
    fresh_after = await probe_blob_action_freshness(page, stale_url, workflow_run_id="wr-2")
    assert fresh_after.state_observed, "rearm must leave a fresh Skyvern-owned state observable"
    assert not fresh_after.retained, "the prior window's blob key must not survive rearm as action-fresh"

    await teardown_blob_url_retention(page, workflow_run_id="wr-2")
    identity = await page.evaluate(
        """() => ({
            createSame: URL.createObjectURL === window.__origCreate,
            revokeSame: URL.revokeObjectURL === window.__origRevoke,
        })"""
    )
    assert identity["createSame"] and identity["revokeSame"], "rearm+teardown must restore the exact native methods"


@pytest.mark.asyncio
async def test_wrapper_mismatch_teardown_neutralizes_orphaned_wrappers(cdp_canonical) -> None:
    # A third party re-wraps the URL methods around Skyvern's wrappers after install. Teardown cannot
    # restore the natives (that would clobber the third party), so it must instead neutralize the
    # captured Skyvern wrappers: after the owned global is dropped, a PDF minted+revoked through the
    # third-party chain must pass through natively (revoked, unreadable), not be retained/deferred into
    # an orphaned bounded map with no future teardown handle.
    page, _ = await cdp_canonical()
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    await page.evaluate(
        """() => {
            const skCreate = URL.createObjectURL;
            const skRevoke = URL.revokeObjectURL;
            URL.createObjectURL = function () { return skCreate.apply(URL, arguments); };
            URL.revokeObjectURL = function () { return skRevoke.apply(URL, arguments); };
            window.__tpCreate = URL.createObjectURL;
            window.__tpRevoke = URL.revokeObjectURL;
        }"""
    )

    await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    state = await page.evaluate(
        """() => ({
            tpCreateKept: URL.createObjectURL === window.__tpCreate,
            tpRevokeKept: URL.revokeObjectURL === window.__tpRevoke,
            skyvernDropped: window.__skyvernBlobRetention === undefined,
        })"""
    )
    assert state["tpCreateKept"] and state["tpRevokeKept"], "teardown must not clobber third-party URL methods"
    assert state["skyvernDropped"], "the owned Skyvern global must be dropped"

    # Mint + synchronous revoke a PDF through the third-party chain (which still calls the captured
    # Skyvern wrappers). With the wrappers neutralized, the revoke is a native pass-through: unreadable.
    orphan_url = await page.evaluate("(n) => window.__mintStatement(n)", "Orphan.pdf")
    assert isinstance(orphan_url, str) and orphan_url.startswith("blob:")
    after = await SkyvernFrame.read_blob_url_bytes(page=page, blob_url=orphan_url, probe=True)
    assert after is None, "a PDF created+revoked through the orphaned chain must pass through natively, not defer"


# ---------------------------------------------------------------------------
# Ownership edge cases: a valid owned stale state must be settled and dropped on rearm even when the page
# has rewrapped one or both URL methods; teardown restores each method independently; and install never
# patches the URL methods unless it can actually publish (own) its state.
# ---------------------------------------------------------------------------


async def _wrap_url_methods(page, which: str) -> None:
    # Wrap the named URL method(s) in a page-owned function that calls the current (Skyvern) method.
    await page.evaluate(
        """(which) => {
            if (which === 'create' || which === 'both') {
                const c = URL.createObjectURL;
                URL.createObjectURL = function () { return c.apply(URL, arguments); };
                window.__pageCreate = URL.createObjectURL;
            }
            if (which === 'revoke' || which === 'both') {
                const r = URL.revokeObjectURL;
                URL.revokeObjectURL = function () { return r.apply(URL, arguments); };
                window.__pageRevoke = URL.revokeObjectURL;
            }
        }""",
        which,
    )


@pytest.mark.asyncio
async def test_stale_owned_with_rewrapped_hooks_is_reset_on_rearm(cdp_canonical) -> None:
    # A prior window installed retention and minted a statement, then the page wrapped BOTH URL methods
    # around Skyvern's wrappers (so a combined wrapper-active check is false). Rearm must still neutralize,
    # settle and drop the stale state — the prior key must not stay fresh — while preserving the page hooks,
    # and the final teardown must restore those pre-rearm (page-hook) methods.
    page, _ = await cdp_canonical()
    await install_blob_url_retention(page, workflow_run_id="wr-1")
    stale_url = await page.evaluate("(n) => window.__mintStatement(n)", "Stale.pdf")
    before = await probe_blob_action_freshness(page, stale_url, workflow_run_id="wr-1")
    assert before.state_observed and before.retained
    await _wrap_url_methods(page, "both")

    await install_blob_url_retention(page, workflow_run_id="wr-2")
    after = await probe_blob_action_freshness(page, stale_url, workflow_run_id="wr-2")
    assert after.state_observed, "rearm must leave a fresh owned state observable"
    assert not after.retained, "the prior window's key must not survive a rewrapped-hook rearm as fresh"

    await teardown_blob_url_retention(page, workflow_run_id="wr-2")
    final = await page.evaluate(
        """() => ({
            createIsPageHook: URL.createObjectURL === window.__pageCreate,
            revokeIsPageHook: URL.revokeObjectURL === window.__pageRevoke,
            dropped: window.__skyvernBlobRetention === undefined,
        })"""
    )
    assert final["createIsPageHook"] and final["revokeIsPageHook"], "teardown must restore the pre-rearm page hooks"
    assert final["dropped"], "the owned state must be dropped after teardown"


@pytest.mark.parametrize("replaced", ["create", "revoke"])
@pytest.mark.asyncio
async def test_one_sided_url_replacement_teardown_restores_independently(cdp_canonical, replaced: str) -> None:
    # Exactly one URL method is replaced by the page after install; the other is still Skyvern's wrapper.
    # Teardown must restore the still-Skyvern method to its native and preserve the page-replaced one.
    page, _ = await cdp_canonical()
    await page.evaluate(
        "() => { window.__origCreate = URL.createObjectURL; window.__origRevoke = URL.revokeObjectURL; }"
    )
    await install_blob_url_retention(page, workflow_run_id="wr-test")
    await _wrap_url_methods(page, replaced)

    await teardown_blob_url_retention(page, workflow_run_id="wr-test")

    r = await page.evaluate(
        """() => ({
            createNative: URL.createObjectURL === window.__origCreate,
            revokeNative: URL.revokeObjectURL === window.__origRevoke,
            createIsPage: URL.createObjectURL === window.__pageCreate,
            revokeIsPage: URL.revokeObjectURL === window.__pageRevoke,
            dropped: window.__skyvernBlobRetention === undefined,
        })"""
    )
    assert r["dropped"]
    if replaced == "create":
        assert r["createIsPage"], "the page-replaced createObjectURL must be preserved"
        assert r["revokeNative"], "the still-Skyvern revokeObjectURL must be restored to native"
    else:
        assert r["revokeIsPage"], "the page-replaced revokeObjectURL must be preserved"
        assert r["createNative"], "the still-Skyvern createObjectURL must be restored to native"


@pytest.mark.parametrize("value", ["null", "undefined"])
@pytest.mark.asyncio
async def test_non_writable_same_name_property_blocks_install_without_side_effects(cdp_canonical, value: str) -> None:
    # A page defines a non-writable, non-configurable same-name property (null/undefined). Install must
    # not patch either URL method and must not modify/delete the foreign property.
    page, _ = await cdp_canonical()
    await page.evaluate(
        """(v) => {
            window.__origCreate = URL.createObjectURL;
            window.__origRevoke = URL.revokeObjectURL;
            Object.defineProperty(window, '__skyvernBlobRetention', {
                value: v === 'null' ? null : undefined, writable: false, configurable: false, enumerable: true,
            });
        }""",
        value,
    )

    await install_blob_url_retention(page, workflow_run_id="wr-test")

    r = await page.evaluate(
        """() => ({
            createUntouched: URL.createObjectURL === window.__origCreate,
            revokeUntouched: URL.revokeObjectURL === window.__origRevoke,
            hasProp: Object.prototype.hasOwnProperty.call(window, '__skyvernBlobRetention'),
            isNull: window.__skyvernBlobRetention === null,
            isUndefined: window.__skyvernBlobRetention === undefined,
        })"""
    )
    assert r["createUntouched"] and r["revokeUntouched"], "install must not patch URL methods when it cannot own state"
    assert r["hasProp"], "the foreign non-writable property must remain"
    assert r["isNull"] if value == "null" else r["isUndefined"], "the foreign property value must be unchanged"


@pytest.mark.asyncio
async def test_partial_url_patch_failure_rolls_back_and_fails_open(cdp_canonical) -> None:
    # revokeObjectURL is frozen non-writable, so install can patch createObjectURL but not revokeObjectURL.
    # Install must detect the partial patch, roll back createObjectURL, drop the just-owned state, and fail
    # open with both methods at their pre-install identities.
    page, _ = await cdp_canonical()
    await page.evaluate(
        """() => {
            window.__origCreate = URL.createObjectURL;
            window.__origRevoke = URL.revokeObjectURL;
            Object.defineProperty(URL, 'revokeObjectURL', {
                value: URL.revokeObjectURL, writable: false, configurable: true,
            });
        }"""
    )

    await install_blob_url_retention(page, workflow_run_id="wr-test")

    r = await page.evaluate(
        """() => ({
            createRolledBack: URL.createObjectURL === window.__origCreate,
            revokeUnchanged: URL.revokeObjectURL === window.__origRevoke,
            dropped: window.__skyvernBlobRetention === undefined,
        })"""
    )
    assert r["createRolledBack"], "createObjectURL must roll back to native after a partial patch"
    assert r["revokeUnchanged"], "the frozen revokeObjectURL must be unchanged"
    assert r["dropped"], "the just-published state must be dropped on rollback"
