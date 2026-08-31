"""E2E regression: pre-action blob URL retention on the pooled / interceptor-bound download path.

Uses real headless Chromium and a local HTTP server. Reproduces the incident shape: a pooled
workflow (``browser_session_id=None``) whose context is bound to a real ``CDPDownloadInterceptor``.
The page mints a PDF ``Blob``, ``URL.createObjectURL``s it to drive an ``<a download>``, and
**synchronously** ``revokeObjectURL``s it. The interceptor recovers the file by reading the blob
bytes back in-page after the download event — which only succeeds if the outer ``handle_action``
armed blob retention (deferring the revoke) before the interaction.

RED on the pinned base: retention is never armed on this path, so the synchronous revoke wins and
no artifact is recovered. GREEN after the fix: exactly one artifact is recovered, no duplicate.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from skyvern.forge.sdk.browser_network_egress_monitor import BrowserNetworkEgressMonitor
from skyvern.forge.sdk.core.http_request_authorization import RunScopedRedirectHopAuthorizer
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import SelectOption, SelectOptionAction
from skyvern.webeye.actions.handler import ActionHandler
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.cdp_download_interceptor import (
    CDPDownloadInterceptor,
    bind_download_interceptor_to_context,
)
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import make_organization, make_step, make_task


def _deps_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _deps_available(),
    reason="Requires Playwright chromium (playwright install chromium)",
)


# A page that mints a PDF blob, drives an <a download>, then synchronously revokes the object URL.
# The revoke is what erases the blob before an out-of-band read on the un-armed path.
_DOWNLOAD_HTML = b"""<!DOCTYPE html>
<html><head><title>Statements</title></head>
<body>
<button id="dl">Download statement</button>
<script>
  document.getElementById('dl').addEventListener('click', function () {
    var body = '%PDF-1.4\\n1 0 obj<</Type/Catalog>>endobj\\ntrailer<</Root 1 0 R>>\\n%%EOF statement-bytes';
    var blob = new Blob([body], { type: 'application/pdf' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'statement.pdf';
    document.body.appendChild(a);
    a.click();
    URL.revokeObjectURL(url);
  });
</script>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_DOWNLOAD_HTML)))
        self.end_headers()
        self.wfile.write(_DOWNLOAD_HTML)

    def log_message(self, *_args: Any) -> None:
        pass


@pytest_asyncio.fixture
async def download_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def _run_pooled_interceptor_download(*, download_dir: Path, server_url: str) -> tuple[list, list[str]]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        await page.goto(server_url)

        interceptor = CDPDownloadInterceptor(
            output_dir=str(download_dir),
            network_egress_monitor=BrowserNetworkEgressMonitor.unenrolled(),
            redirect_hop_authorizer=RunScopedRedirectHopAuthorizer("e2e-run"),
        )
        for existing_page in context.pages:
            # An unenrolled egress monitor refuses per-page Fetch interception by design; production
            # swallows this and relies on the browser-level download monitor below, which is the path
            # a client-side blob download actually takes.
            try:
                await interceptor.enable_for_page(existing_page)
            except Exception:
                pass
        await bind_download_interceptor_to_context(interceptor, context)
        await interceptor.enable_browser_download_monitor(browser, context)

        now = datetime.now(UTC)
        organization = make_organization(now)
        task = make_task(
            now,
            organization,
            workflow_run_id="e2e-run",
            browser_session_id=None,
            download_timeout=6.0,
        )
        step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

        action = SelectOptionAction(
            element_id="download-select",
            option=SelectOption(label="statement", value="statement"),
            download=True,
            organization_id=task.organization_id,
            task_id=task.task_id,
            step_id=step.step_id,
        )

        browser_state = MagicMock()
        browser_state.browser_artifacts = MagicMock(remote_browser_session_id=None)
        browser_state.release_driver_on_close = False
        browser_state.list_valid_pages = AsyncMock(return_value=[page])

        scraped_page = ScrapedPage(
            elements=[],
            element_tree=[],
            element_tree_trimmed=[],
            _browser_state=browser_state,
            _clean_up_func=AsyncMock(return_value=[]),
            _scrape_exclude=None,
        )

        async def click_download(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
            await page.click("#dl")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        try:
            with (
                patch.object(ActionHandler, "_handle_action", side_effect=click_download),
                patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(download_dir)),
                patch(
                    "skyvern.webeye.actions.handler.skyvern_context.current",
                    return_value=MagicMock(run_id="e2e-run", download_suffix=None),
                ),
                patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 3.0),
                patch("skyvern.webeye.actions.handler.app", mock_app),
            ):
                results = await ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )
        finally:
            await context.close()
            await browser.close()

    files_on_disk = [str(p) for p in Path(download_dir).iterdir() if p.is_file()]
    return results, files_on_disk


@pytest.mark.asyncio
async def test_pooled_interceptor_select_download_retains_synchronously_revoked_blob(
    tmp_path: Path, download_server: str
) -> None:
    download_dir = tmp_path / "run-downloads"
    download_dir.mkdir()

    results, files_on_disk = await _run_pooled_interceptor_download(
        download_dir=download_dir, server_url=download_server
    )

    # Exactly one artifact recovered from the synchronously-revoked blob, and it is non-empty.
    assert len(files_on_disk) == 1, files_on_disk
    assert Path(files_on_disk[0]).stat().st_size > 0
    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files is not None
    assert len(results[-1].downloaded_files) == 1
