"""SKY-15371 load-bearing regression: real-Chromium artifact-shape download-popup lifecycle.

Reproduces the deployed-recurrence anchor shape (artifact-forensics/report.md) end to end through the
real shipped seams: an ordinary ``download=false`` CLICK synchronously ``window.open``s a new popup in
the initiating page's BrowserContext; no Playwright ``download`` event ever fires (the file is credited
later by the file-scan lifecycle); and the wedged popup is absent from the scraper-oriented
``list_valid_pages()``. The download is then durably credited and the credit seam must close exactly
that action-owned popup while leaving the opener open.

The test drives ``ActionHandler.handle_action``'s real false-click producer (which arms
``page.on("popup", ...)`` and records the task-scoped claim) over real headless Chromium, then the real
``ForgeAgent._close_credited_download_popups`` credit consumer. Real Chromium supplies the
artifact-shaped popup (a genuine same-context Page, event-free); only the browser-manager lookup, the
download directory, and the durable-credit ``browser_state`` (its ``list_valid_pages`` /
``browser_context``) are stubbed -- the remote/file-credit nondeterminism the contract permits.

An inline ``application/pdf`` popup would fire a Playwright ``download`` event under a plain headless
Chromium (the CDP interceptor that suppresses it in production is not wired here), defeating the
no-download-event shape; a rendered HTML viewer that commits to a real URL reproduces the lingering,
event-free popup deterministically and also proves URL/commit state is not a cleanup filter.

The credit consumer must close the wedged popup even though the scraper-oriented ``list_valid_pages()``
omits it: eligibility gates on ``popup.context is browser_state.browser_context``, not scraper-list
membership.
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

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import ClickAction
from skyvern.webeye.actions.handler import ActionHandler
from skyvern.webeye.actions.responses import ActionSuccess
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

# A viewer document for the opened popup. Rendered inline (no Content-Disposition), so Chromium keeps
# it as an open Page and fires no Playwright download event.
_DOC_HTML = b"<!doctype html><html><head><title>Receipt</title></head><body>Receipt document</body></html>"
# A synchronous window.open in the click handler -- the producer shape of the anchor incident.
_OPENER_HTML = (
    b"<!doctype html><html><head><title>documents</title></head>"
    b"<body><button id='dl' onclick=\"window.open('/doc','_blank')\">download</button></body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        body = _DOC_HTML if self.path.startswith("/doc") else _OPENER_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest_asyncio.fixture
async def popup_site() -> Any:
    """Real Chromium context with an opener page on a local origin, plus a download-event recorder."""
    from playwright.async_api import async_playwright

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-proxy-server", "--proxy-bypass-list=*"],
            proxy={"server": "direct://"},
        )
        context = await browser.new_context(accept_downloads=True)
        downloads: list[Any] = []
        context.on("download", lambda d: downloads.append(d))
        opener = await context.new_page()
        await opener.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        try:
            yield context, opener, downloads
        finally:
            await browser.close()
            server.shutdown()


@pytest.mark.asyncio
async def test_action_owned_popup_closed_after_late_download_credit(popup_site: Any, tmp_path: Path) -> None:
    context, opener, downloads = popup_site
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="task-popup", workflow_run_id="wr-popup")
    step = make_step(now, task, step_id="step-popup", status=StepStatus.created, order=0, output=None)
    action = ClickAction(element_id="download-link", download=False)
    owning_ctx = SkyvernContext(task_id=task.task_id)

    scraped_page = MagicMock()
    seam_browser_state = MagicMock()
    seam_browser_state.browser_artifacts = MagicMock(needs_cdp_frame_publisher=False, remote_browser_session_id=None)
    seam_browser_state.release_driver_on_close = False

    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = seam_browser_state
    app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)

    captured_popup: list[Any] = []

    async def inner(*args: object, **kwargs: object) -> list[ActionSuccess]:
        pg = kwargs["page"]
        # Synchronous window.open toward a committing document: the real page.on("popup") producer
        # armed by handle_action fires, and no Playwright download event is ever raised.
        async with pg.expect_popup() as popup_info:
            await pg.evaluate("window.open('/doc','_blank')")
        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded")
        captured_popup.append(popup)
        return [ActionSuccess()]

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=inner),
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.get_download_dir", MagicMock(return_value=str(tmp_path))),
        patch("skyvern.webeye.actions.handler.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.0),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=owning_ctx),
    ):
        await ActionHandler.handle_action(
            scraped_page,
            task,
            step,
            opener,
            action,
            file_download_false_click_eligible=True,
        )

    # The artifact shape produced by the real action seam: a live committed same-context popup, no
    # Playwright download event, opener still open.
    assert captured_popup, "the false-click action did not open a popup"
    popup = captured_popup[0]
    assert not popup.is_closed(), "setup: popup must be open before credit"
    assert not opener.is_closed(), "setup: opener must be open before credit"
    assert downloads == [], "setup: inline document must not fire a Playwright download event"
    assert popup.url.endswith("/doc"), "setup: popup committed to a real URL (URL is not a cleanup filter)"
    assert popup.context is context, "setup: popup belongs to the initiating BrowserContext"
    # Producer sanity: the real false-click seam recorded the action-owned popup as a task claim.
    recorded = owning_ctx.download_popup_claims.get(task.task_id, [])
    assert any(candidate is popup for candidate in recorded), "the false-click producer did not record the popup claim"

    # Durable credit arrives later (file-scan lifecycle). The scraper-oriented list omits the wedged
    # popup (as in the anchor), but the credit consumer must still close exactly the action-owned popup.
    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context
    credit_browser_state.list_valid_pages = AsyncMock(return_value=[opener])

    agent = ForgeAgent()
    with patch("skyvern.forge.agent.skyvern_context.current", return_value=owning_ctx):
        await agent._close_credited_download_popups(task, credit_browser_state)

    assert popup.is_closed(), "action-owned download popup was left open after durable credit (SKY-15371 wedge)"
    assert not opener.is_closed(), "the opener page must remain open"
